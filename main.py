import os
import re
import random
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

# --- 1. YANGILANGAN VA TOʻGʻRILANGAN PARSER TIZIMI ---
def process_raw_text(content: str) -> dict:
    """
    Matnni aniq format bo'yicha tahlil qiladi:
    ++++ dan keyingi qator -> Savol matni
    ==== dan keyingi qator -> Variant matni
    Agar variant matni # bilan boshlansa -> Bu to'g'ri javob
    # Fan_Nomi -> Fan nomi (faqat variant kutish holatida bo'lmaganda)
    """
    subjects = {}
    current_subject = "Falsafa"  # Standart fan nomi
    questions = []
    current_q = None
    
    expecting_question_text = False
    expecting_option_text = False
    
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue  # Bo'sh qatorlarni tashlab ketamiz
            
        # 1. Savol boshlanishi signali (++++)
        if line.startswith('++++'):
            if current_q and len(current_q['options']) >= 2:
                questions.append(current_q)
            current_q = {'question': '', 'options': [], 'correct_idx': 0}
            expecting_question_text = True
            expecting_option_text = False
            continue
            
        # 2. Variant boshlanishi signali (====)
        if line.startswith('===='):
            expecting_question_text = False
            expecting_option_text = True
            continue
            
        # 3. Fan nomini aniqlash (# Fan Nomi) - faqat variant matni kutilmayotgan holatda
        if line.startswith('#') and not expecting_option_text:
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
            
            expecting_question_text = False
            expecting_option_text = False
            continue
            
        # 4. Savol matnini o'qib olish
        if expecting_question_text and current_q:
            current_q['question'] = line[:300]
            expecting_question_text = False
            continue
            
        # 5. Variant matnini o'qib olish (To'g'ri va noto'g'riligini tekshirish)
        if expecting_option_text and current_q:
            if line.startswith('#'):
                # Agar javob # bilan boshlansa, uni to'g'ri javob indeksi qilib belgilaymiz
                current_q['correct_idx'] = len(current_q['options'])
                option_text = line[1:].strip()  # # belgisini olib tashlaymiz (toza matn qoladi)
            else:
                option_text = line
                
            current_q['options'].append(option_text[:100])
            expecting_option_text = False
            continue

    # Oxirgi savolni ham saqlab qo'yamiz
    if current_q and len(current_q['options']) >= 2:
        questions.append(current_q)
        
    if questions:
        if current_subject not in subjects:
            subjects[current_subject] = []
        subjects[current_subject].extend(questions)
        
    return subjects

# --- 2. MA'LUMOTLAR BAZASI VA FAILLAR BILAN ISHLASH ---
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
                        sf.write("++++\n")
                        sf.write(f"{q['question']}\n\n")
                        for idx, opt in enumerate(q['options']):
                            sf.write("====\n")
                            if idx == q['correct_idx']:
                                sf.write(f"# {opt}\n\n")
                            else:
                                sf.write(f"{opt}\n\n")
            logging.info("✅ questions.txt aniq formatda muvaffaqiyatli saqlandi!")
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
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    parsed = process_raw_text(content)
    all_qs = []
    for qs in parsed.values():
        all_qs.extend(qs)
    return all_qs

def chunk_questions(questions, size=30):
    return [questions[i:i + size] for i in range(0, len(questions), size)]

# --- 3. BUYRUQLAR VA MENYU (HANDLERS) ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer("👋 Assalomu alaykum!\n\nTo'g'rilangan chiziqli formatdagi test botiga xush kelibsiz.\n👉 Testni boshlash uchun /test buyrug'ini bosing.")

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
        await message.answer("⚠️ Tizimda hali biron bir fan topilmadi! questions.txt faylini tekshiring.")
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

# --- 4. TEST JARAYONI VA RANDOM SHUFFLE ---
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
    await call.message.edit_text(f"🚀 <b>{subject_name.replace('_', ' ')}: {chunk_index + 1}-bo'lim boshlandi!</b>\nSavollar soni: {len(chunk)} ta.\n💡 <i>Javob variantlari o'rni tasodifiy almashtirildi!</i>")
    
    asyncio.create_task(send_test_chunk(call.message.chat.id, chunk, call.message.chat.type))

async def send_test_chunk(chat_id: int, chunk: list, chat_type: str = "group"):
    active_tests[chat_id] = True 
    session_scores[chat_id] = {}
    
    async with aiosqlite.connect(DB_PATH) as db:
        for q in chunk:
            if not active_tests.get(chat_id):
                session_scores.pop(chat_id, None)
                return
            if len(q['options']) < 2: continue
            
            # Variantlarni joyini almashtirib yuborish (Shuffle)
            indexed_options = list(enumerate(q['options']))
            random.shuffle(indexed_options)
            
            shuffled_options = [opt for idx, opt in indexed_options]
            correct_idx = next(i for i, (idx, opt) in enumerate(indexed_options) if idx == q['correct_idx'])

            try:
                msg = await bot.send_poll(
                    chat_id=chat_id, question=q['question'], options=shuffled_options,
                    type='quiz', correct_option_id=correct_idx, is_anonymous=False, open_period=30
                )
                await db.execute("INSERT OR REPLACE INTO active_polls (poll_id, correct_option_id, chat_id) VALUES (?, ?, ?)", 
                                 (msg.poll.id, correct_idx, chat_id))
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
        await message.answer("📭 Guruhda hali reyting shakllanmagan.")
        return
    text = "<b>🏆 Umumiy Reyting (Top 15):</b>\n\n"
    for i, (name, score) in enumerate(users, 1):
        text += f"{i}. {name} — {score} ball\n"
    await message.answer(text)

# --- 5. JAVOBLARNI TEKSHIRISH ---
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
