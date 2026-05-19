import os
import re
import asyncio
import logging
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "bot_db.sqlite"
SUBJECTS_DIR = "subjects"  # Ajratilgan fanlar saqlanadigan papka

# Faol testlarni nazorat qilish va vaqtinchalik natijalar
active_tests = {}
private_chat_events = {}
session_scores = {}

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- 1. AQLLI PARSER (HAR XIL RAQAMLAR VA '#' BELGISI BILAN ISHLASH) ---
def process_raw_text(content: str) -> dict:
    subjects = {}
    current_subject = "Falsafa"  # Boshida fani yozilmagan bo'lsa standart nom
    questions = []
    current_q = None
    
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # 1. Yangi fan aniqlash (# Fan nomi)
        if line.startswith('#'):
            if current_q and len(current_q['options']) >= 2:
                questions.append(current_q)
                current_q = None
            if questions:
                if current_subject not in subjects:
                    subjects[current_subject] = []
                subjects[current_subject].extend(questions)
                questions = []
            
            current_subject = line[1:].strip().replace(" ", "_")
            current_subject = re.sub(r'[\\/*?:"<>|]', "", current_subject)
            if not current_subject:
                current_subject = "Umumiy"
            continue
            
        # 2. Tizim formati (? Savol)
        if line.startswith('?'):
            if current_q and len(current_q['options']) >= 2:
                questions.append(current_q)
            current_q = {'question': line[1:].strip()[:300], 'options': [], 'correct_idx': 0}
            continue
            
        # 3. To'g'ri javob belgisi (== Javob)
        if line.startswith('==') and current_q:
            current_q['correct_idx'] = len(current_q['options'])
            current_q['options'].append(line[2:].strip()[:100])
            continue
            
        # 4. Oddiy javob belgisi (= Javob)
        if line.startswith('=') and current_q:
            current_q['options'].append(line[1:].strip()[:100])
            continue
            
        # 5. Standart variantlar formati (A, B, C, D) va to'g'ri javobni (* yoki +) aniqlash
        option_match = re.match(r'^([\*\+==]*)\s*([A-Da-d])[\.\)\s]+(.*)', line)
        if option_match and current_q:
            is_correct = bool(option_match.group(1)) or '*' in line[:3] or '+' in line[:3]
            option_text = option_match.group(3).strip()[:100]
            if is_correct:
                current_q['correct_idx'] = len(current_q['options'])
            current_q['options'].append(option_text)
            continue
            
        # 6. Har xil va tartibsiz kelgan savol raqamlari (Masalan: 1., 354., 410), 12) va hokazo)
        question_match = re.match(r'^\d+[\.\)\s]+(.*)', line)
        if question_match:
            if current_q and len(current_q['options']) >= 2:
                questions.append(current_q)
            current_q = {'question': question_match.group(1).strip()[:300], 'options': [], 'correct_idx': 0}
            continue
            
        # 7. Shunchaki matn davomi bo'lsa
        if len(line) > 5 and not any(w in line.upper() for w in ["VAZIRLIGI", "INSTITUTI", "KAFEDRASI", "TESTLAR"]):
            if current_q and len(current_q['options']) == 0:
                current_q['question'] = (current_q['question'] + " " + line)[:300]
            else:
                if current_q and len(current_q['options']) >= 2:
                    questions.append(current_q)
                current_q = {'question': line[:300], 'options': [], 'correct_idx': 0}
                
    if current_q and len(current_q['options']) >= 2:
        questions.append(current_q)
        
    if questions:
        if current_subject not in subjects:
            subjects[current_subject] = []
        subjects[current_subject].extend(questions)
        
    return subjects

# --- 2. MA'LUMOTLAR BAZASI VA FANLARNI SINKRONIZATSIYA QILISH ---
async def init_db():
    os.makedirs(SUBJECTS_DIR, exist_ok=True)
    
    if os.path.exists("questions.txt"):
        try:
            with open("questions.txt", "r", encoding="utf-8") as f:
                content = f.read()
            
            parsed_subjects = process_raw_text(content)
            for sub_name, qs in parsed_subjects.items():
                filepath = os.path.join(SUBJECTS_DIR, f"{sub_name}.txt")
                with open(filepath, "w", encoding="utf-8") as sf:
                    for q in qs:
                        sf.write(f"? {q['question']}\n")
                        for idx, opt in enumerate(q['options']):
                            prefix = "==" if idx == q['correct_idx'] else "="
                            sf.write(f"{prefix} {opt}\n")
                        sf.write("\n")
            logging.info("✅ questions.txt muvaffaqiyatli fanlarga ajratildi!")
        except Exception as e:
            logging.error(f"❌ questions.txt o'qishda xatolik: {e}")

    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                user_id INTEGER,
                chat_id INTEGER,
                full_name TEXT,
                score INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_polls (
                poll_id TEXT PRIMARY KEY,
                correct_option_id INTEGER,
                chat_id INTEGER
            )
        """)
        await db.commit()

def get_all_subjects():
    if not os.path.exists(SUBJECTS_DIR):
        return []
    files = os.listdir(SUBJECTS_DIR)
    return [os.path.splitext(f)[0] for f in files if f.endswith('.txt')]

def parse_subject_questions(subject_name):
    filepath = os.path.join(SUBJECTS_DIR, f"{subject_name}.txt")
    if not os.path.exists(filepath):
        return []
    
    questions = []
    current_q = None
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            if line.startswith('?'):
                if current_q: questions.append(current_q)
                current_q = {'question': line[1:].strip(), 'options': [], 'correct_idx': 0}
            elif line.startswith('=='):
                if current_q:
                    current_q['correct_idx'] = len(current_q['options'])
                    current_q['options'].append(line[2:].strip())
            elif line.startswith('='):
                if current_q:
                    current_q['options'].append(line[1:].strip())
    if current_q: questions.append(current_q)
    return questions

def chunk_questions(questions, size=30):
    return [questions[i:i + size] for i in range(0, len(questions), size)]

# --- 3. BUYRUQLAR VA MENYU (HANDLERS) ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer("👋 Assalomu alaykum!\n\nTuzatilgan va yangilangan aqlli test botiga xush kelibsiz.\n👉 Testni boshlash uchun /test buyrug'ini bosing.")

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    text = (
        "<b>🤖 Bot buyruqlari:</b>\n\n"
        "🔸 /test - Fanlar ro'yxatini ko'rish va testni boshlash\n"
        "🔸 /leaderboard - Umumiy reytingni ko'rish\n"
        "🔸 /stop - Faol testni to'xtatish"
    )
    await message.answer(text)

@dp.message(Command("test"))
async def send_subjects_menu(message: types.Message):
    subjects = get_all_subjects()
    if not subjects:
        await message.answer("⚠️ Tizimda hali biron bir fan topilmadi!")
        return
    
    builder = InlineKeyboardBuilder()
    for sub in subjects:
        builder.button(text=f"📚 {sub.replace('_', ' ')}", callback_data=f"sub_{sub}")
    builder.adjust(2)
    await message.answer("🔍 <b>Qaysi fandan test topshirmoqchisiz?</b>", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("sub_"))
async def handle_subject_selection(call: types.CallbackQuery):
    subject_name = call.data.replace("sub_", "")
    questions = parse_subject_questions(subject_name)
    
    if not questions:
        await call.answer("⚠️ Ushbu fanda savollar topilmadi!", show_alert=True)
        return
        
    chunks = chunk_questions(questions, 30)
    builder = InlineKeyboardBuilder()
    for i, chunk in enumerate(chunks):
        start_num = (i * 30) + 1
        end_num = start_num + len(chunk) - 1
        builder.button(text=f"{i + 1}-bo'lim ({start_num}-{end_num})", callback_data=f"run_{subject_name}_{i}")
    
    builder.button(text="⬅️ Orqaga", callback_data="back_to_subs")
    builder.adjust(2)
    
    await call.message.edit_text(
        f"📖 <b>{subject_name.replace('_', ' ')} fani.</b> (Jami: {len(questions)} ta savol)\n\nBo'limni tanlang:",
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "back_to_subs")
async def back_to_subs(call: types.CallbackQuery):
    subjects = get_all_subjects()
    builder = InlineKeyboardBuilder()
    for sub in subjects:
        builder.button(text=f"📚 {sub.replace('_', ' ')}", callback_data=f"sub_{sub}")
    builder.adjust(2)
    await call.message.edit_text("🔍 <b>Qaysi fandan test topshirmoqchisiz?</b>", reply_markup=builder.as_markup())

# --- 4. TEST JARAYONI (XATOLIK TUZATILGAN JOYI) ---
@dp.callback_query(F.data.startswith("run_"))
async def handle_run_test(call: types.CallbackQuery):
    if active_tests.get(call.message.chat.id):
        await call.answer("⚠️ Chatda faol test mavjud. Uni to'xtatish uchun /stop buyrug'ini bering.", show_alert=True)
        return

    data_parts = call.data.split("_")
    chunk_index = int(data_parts[-1])
    subject_name = "_".join(data_parts[1:-1])
    
    questions = parse_subject_questions(subject_name)
    chunks = chunk_questions(questions, 30)
    
    if chunk_index >= len(chunks):
        await call.answer("Bo'lim topilmadi.", show_alert=True)
        return
        
    chunk = chunks[chunk_index]
    await call.message.edit_text(f"🚀 <b>{subject_name.replace('_', ' ')}: {chunk_index + 1}-bo'lim boshlandi!</b>\nSavollar soni: {len(chunk)} ta.")
    
    # Bu yerda chat turi ham uzatiladi
    asyncio.create_task(send_test_chunk(call.message.chat.id, chunk, call.message.chat.type))

# chat_type parametriga standart qiymat berildi (Xatolik butkul yo'qoldi)
async def send_test_chunk(chat_id: int, chunk: list, chat_type: str = "group"):
    active_tests[chat_id] = True 
    session_scores[chat_id] = {}
    
    async with aiosqlite.connect(DB_PATH) as db:
        for q in chunk:
            if not active_tests.get(chat_id):
                session_scores.pop(chat_id, None)
                return
            if len(q['options']) < 2: continue
            
            try:
                msg = await bot.send_poll(
                    chat_id=chat_id, question=q['question'], options=q['options'],
                    type='quiz', correct_option_id=q['correct_idx'], is_anonymous=False, open_period=30
                )
                await db.execute("INSERT OR REPLACE INTO active_polls (poll_id, correct_option_id, chat_id) VALUES (?, ?, ?)", 
                                 (msg.poll.id, q['correct_idx'], chat_id))
                await db.commit()
            except Exception as e:
                logging.error(f"Poll yuborishda xatolik: {e}")
                continue

            if chat_type == "private":
                event = asyncio.Event()
                private_chat_events[chat_id] = event
                try: await asyncio.wait_for(event.wait(), timeout=30.0)
                except asyncio.TimeoutError: pass  
                finally: private_chat_events.pop(chat_id, None)
            else:
                for _ in range(30):
                    if not active_tests.get(chat_id): break
                    await asyncio.sleep(1)
                    
            try: await bot.stop_poll(chat_id=chat_id, message_id=msg.message_id)
            except: pass
                
    if active_tests.get(chat_id):
        current_results = session_scores.pop(chat_id, None)
        results_text = ""
        if current_results:
            sorted_results = sorted(current_results.values(), key=lambda x: x['score'], reverse=True)
            results_text = "\n📊 <b>Ushbu bo'lim natijalari:</b>\n\n"
            for idx, res in enumerate(sorted_results, 1):
                results_text += f"{idx}. {res['name']} — {res['score']} ta to'g'ri\n"
        else:
            results_text = "\n📊 Afsuski, hech kim to'g'ri javob bermadi."

        await bot.send_message(chat_id, f"✅ Bo'lim yakunlandi!\n{results_text}\nℹ️ /leaderboard")
        active_tests.pop(chat_id, None)

@dp.message(Command("stop"))
async def stop_test_cmd(message: types.Message):
    chat_id = message.chat.id
    if active_tests.get(chat_id):
        active_tests[chat_id] = False
        if chat_id in private_chat_events: private_chat_events[chat_id].set()
        session_scores.pop(chat_id, None)
        await message.answer("🛑 <b>Test jarayoni muvaffaqiyatli to'xtatildi!</b>")
    else:
        await message.answer("⚠️ Hozirda hech qanday faol test yo'q.")

@dp.message(Command("leaderboard"))
async def show_leaderboard(message: types.Message):
    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT full_name, score FROM scores WHERE chat_id = ? ORDER BY score DESC LIMIT 15", (chat_id,)) as cursor:
            users = await cursor.fetchall()
    if not users:
        await message.answer("📭 Ushbu guruhda hali reyting shakllanmagan.")
        return
    text = "<b>🏆 Umumiy Reyting (Top 15):</b>\n\n"
    for i, (name, score) in enumerate(users, 1):
        text += f"{i}. {name} — {score} ball\n"
    await message.answer(text)

# --- 5. JAVOBLARNY TEKSHIRISH ---
@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    full_name = poll_answer.user.first_name + (f" {poll_answer.user.last_name}" if poll_answer.user.last_name else "")
    
    if not poll_answer.option_ids: return
    selected_option = poll_answer.option_ids[0]

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT correct_option_id, chat_id FROM active_polls WHERE poll_id = ?", (poll_id,)) as cursor:
            row = await cursor.fetchone()
        if row:
            correct_option, chat_id = row
            if selected_option == correct_option:
                await db.execute("""
                    INSERT INTO scores (user_id, chat_id, full_name, score) VALUES (?, ?, ?, 1)
                    ON CONFLICT(user_id, chat_id) DO UPDATE SET score = score + 1, full_name = excluded.full_name
                """, (user_id, chat_id, full_name))
                await db.commit()
                
                if chat_id in session_scores:
                    if user_id not in session_scores[chat_id]:
                        session_scores[chat_id][user_id] = {"name": full_name, "score": 0}
                    session_scores[chat_id][user_id]["score"] += 1
            
            if chat_id in private_chat_events: 
                private_chat_events[chat_id].set()

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
