import os
import re
import asyncio
import logging
import aiosqlite
import shutil
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "bot_db.sqlite"
SUBJECTS_DIR = "subjects"  # Fanlar saqlanadigan papka

# Faol testlarni nazorat qilish
active_tests = {}
# Shaxsiy chatlarda taymerni muddatidan oldin uyg'otish uchun hodisalar lug'ati
private_chat_events = {}
# Joriy bo'lim (sessiya) natijalarini vaqtinchalik saqlash uchun lug'at
session_scores = {}

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- 1. MA'LUMOTLAR BAZASINI VA PAPKALARNI YARATISH ---
async def init_db():
    os.makedirs(SUBJECTS_DIR, exist_ok=True)
    
    if os.path.exists("questions.txt") and not os.listdir(SUBJECTS_DIR):
        try:
            shutil.copy("questions.txt", os.path.join(SUBJECTS_DIR, "Falsafa.txt"))
        except Exception as e:
            logging.error(f"Eski faylni ko'chirishda xatolik: {e}")

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

# --- 2. FANLAR BILAN ISHLASH VA AQLLI PARSER ---
def get_all_subjects():
    if not os.path.exists(SUBJECTS_DIR):
        return []
    files = os.listdir(SUBJECTS_DIR)
    return [os.path.splitext(f)[0] for f in files if f.endswith('.txt')]

def process_raw_text(content: str) -> list:
    questions = []
    current_q = None
    lines = content.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Tizim formati (?, ==, =)
        if line.startswith('?'):
            if current_q and len(current_q['options']) >= 2:
                questions.append(current_q)
            current_q = {'question': line[1:].strip()[:300], 'options': [], 'correct_idx': 0}
            continue
        elif line.startswith('==') and current_q:
            current_q['correct_idx'] = len(current_q['options'])
            current_q['options'].append(line[2:].strip()[:100])
            continue
        elif line.startswith('=') and current_q:
            current_q['options'].append(line[1:].strip()[:100])
            continue
            
        # Standart variantlar formati (A, B, C, D) va to'g'ri javobni (* yoki +) aniqlash
        option_match = re.match(r'^([\*\+==]*)\s*([A-Da-d]|[0-9])[\.\)\s]+(.*)', line)
        if option_match:
            if current_q:
                is_correct = bool(option_match.group(1)) or '*' in line[:4] or '+' in line[:4]
                option_text = option_match.group(3).strip()[:100]
                if is_correct:
                    current_q['correct_idx'] = len(current_q['options'])
                current_q['options'].append(option_text)
            continue
            
        # Savol raqami (Masalan: 1. Savol matni)
        question_match = re.match(r'^\d+[\.\)\s]+(.*)', line)
        if question_match:
            if current_q and len(current_q['options']) >= 2:
                questions.append(current_q)
            current_q = {'question': question_match.group(1).strip()[:300], 'options': [], 'correct_idx': 0}
            continue
            
        # Shunchaki matn davomi bo'lsa
        if len(line) > 5 and not any(w in line.upper() for w in ["VAZIRLIGI", "INSTITUTI", "KAFEDRASI", "TESTLAR"]):
            if current_q and len(current_q['options']) == 0:
                current_q['question'] = (current_q['question'] + " " + line)[:300]
            else:
                if current_q and len(current_q['options']) >= 2:
                    questions.append(current_q)
                current_q = {'question': line[:300], 'options': [], 'correct_idx': 0}
                
    if current_q and len(current_q['options']) >= 2:
        questions.append(current_q)
        
    return questions

def parse_subject_questions(subject_name):
    filepath = os.path.join(SUBJECTS_DIR, f"{subject_name}.txt")
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    return process_raw_text(content)

def chunk_questions(questions, size=30):
    return [questions[i:i + size] for i in range(0, len(questions), size)]

# --- 3. BUYRUQLAR (HANDLERS) ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer("👋 Assalomu alaykum!\n\nMen Word (.docx) fayllarni ham qabul qila oladigan aqlli test botiman.\n👉 Testni boshlash uchun /test buyrug'ini bosing.")

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    text = (
        "<b>🤖 Bot buyruqlari:</b>\n\n"
        "🔸 /test - Fanlar ro'yxati va testni boshlash\n"
        "🔸 /addtest - Matn ko'rinishida yangi test qo'shish\n"
        "📎 <b>Word fayl qo'shish:</b> Shunchaki botga <code>.docx</code> formatidagi faylni yuboring (Fayl nomi fan nomi bo'ladi)."
    )
    await message.answer(text)

# --- 📥 WORD (.DOCX) FAYLLARNI QABUL QILISH FUNKSIYASI ---
@dp.message(F.document)
async def handle_docx_document(message: types.Message):
    document = message.document
    file_name = document.file_name
    
    if not file_name.lower().endswith('.docx'):
        await message.answer("⚠️ Iltimos, faqat Word (<b>.docx</b>) formatidagi fayllarni yuboring.")
        return
        
    # Fayl nomidan fan nomini ajratib olamiz va noqonuniy belgilarni tozalaymiz
    subject_name = os.path.splitext(file_name)[0]
    subject_name = re.sub(r'[\\/*?:"<>|]', "", subject_name).replace(" ", "_")
    
    status_msg = await message.answer("📥 Word fayli yuklab olinmoqda va tahlil qilinmoqda, iltimos kuting...")
    
    file_id = document.file_id
    file = await bot.get_file(file_id)
    
    # Vaqtinchalik fayl yaratish
    temp_path = f"temp_{file_id}.docx"
    await bot.download_file(file.file_path, temp_path)
    
    try:
        import docx  # python-docx kutubxonasi
        doc = docx.Document(temp_path)
        
        # Word ichidagi barcha qatorlarni bitta matnga birlashtiramiz
        text_content = "\n".join([p.text for p in doc.paragraphs])
        
        parsed_qs = process_raw_text(text_content)
        
        if not parsed_qs:
            await status_msg.edit_text("❌ Fayl ichidan testlar topilmadi. To'g'ri javoblar oldiga * yoki + qo'yilganini tekshiring.")
            os.remove(temp_path)
            return
            
        # Olingan testlarni fanning fayliga saqlaymiz
        filepath = os.path.join(SUBJECTS_DIR, f"{subject_name}.txt")
        with open(filepath, "a", encoding="utf-8") as f:
            for q in parsed_qs:
                f.write(f"? {q['question']}\n")
                for idx, opt in enumerate(q['options']):
                    prefix = "==" if idx == q['correct_idx'] else "="
                    f.write(f"{prefix} {opt}\n")
                f.write("\n")
                
        await status_msg.edit_text(
            f"✅ <b>Muvaffaqiyatli yuklandi!</b>\n\n"
            f"📚 Fan nomi: <b>{subject_name.replace('_', ' ')}</b>\n"
            f"📝 Aniqlangan testlar: <b>{len(parsed_qs)} ta savol</b>\n\n"
            f"O'quvchilar endi /test buyrug'i orqali ushbu fandan test yechishlari mumkin!"
        )
    except ImportError:
        await status_msg.edit_text("❌ Serverda xatolik: <code>python-docx</code> kutubxonasi o'rnatilmagan!")
    except Exception as e:
        await status_msg.edit_text(f"❌ Faylni o'qishda xatolik yuz berdi: {e}")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

# --- MATN KO'RINISHIDA TEST QO'SHISH ---
@dp.message(Command("addtest"))
async def add_test_via_tg(message: types.Message):
    text_after_cmd = message.text.replace("/addtest", "").strip()
    lines = text_after_cmd.split('\n')
    subject_name = lines[0].strip() if lines else ""
    
    if not text_after_cmd or not subject_name or len(lines) < 2:
        example = (
            "📝 <b>Yangi fanga matn orqali test qo'shish formati:</b>\n\n"
            "<code>/addtest Tarix</code> (Birinchi qatorga faqat fan nomi)\n"
            "<code>1. O'zbekiston poytaxti qayer?</code>\n"
            "<code>A) Samarqand</code>\n"
            "<code>*B) Toshkent</code>\n"
            "<code>C) Buxoro</code>"
        )
        await message.answer(example)
        return

    test_content = "\n".join(lines[1:]).strip()
    parsed_qs = process_raw_text(test_content)
    
    if not parsed_qs:
        await message.answer("❌ Matnni tahlil qilib bo'lmadi.")
        return

    subject_name = re.sub(r'[\\/*?:"<>|]', "", subject_name).replace(" ", "_")
    filepath = os.path.join(SUBJECTS_DIR, f"{subject_name}.txt")

    with open(filepath, "a", encoding="utf-8") as f:
        for q in parsed_qs:
            f.write(f"? {q['question']}\n")
            for idx, opt in enumerate(q['options']):
                prefix = "==" if idx == q['correct_idx'] else "="
                f.write(f"{prefix} {opt}\n")
            f.write("\n")

    await message.answer(f"✅ <b>{subject_name}</b> faniga {len(parsed_qs)} ta savol qo'shildi!")

# --- FANLAR RO'YXATI ---
@dp.message(Command("test"))
async def send_subjects_menu(message: types.Message):
    subjects = get_all_subjects()
    if not subjects:
        await message.answer("⚠️ Tizimda hali biron bir fan yo'q! Word (.docx) fayl yuboring yoki /addtest buyrug'idan foydalaning.")
        return
    
    builder = InlineKeyboardBuilder()
    for sub in subjects:
        builder.button(text=f"📚 {sub.replace('_', ' ')}", callback_data=f"sub_{sub}")
    builder.adjust(2)
    await message.answer("🔍 <b>Qaysi fandan test topshirmoqchisiz?</b>", reply_markup=builder.as_markup())

# --- BO'LIMLAR RO'YXATI ---
@dp.callback_query(F.data.startswith("sub_"))
async def handle_subject_selection(call: types.CallbackQuery):
    subject_name = call.data.replace("sub_", "")
    questions = parse_subject_questions(subject_name)
    
    if not questions:
        await call.answer("⚠️ Savollar topilmadi!", show_alert=True)
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

# --- TESTNI BOSHLASH ---
@dp.callback_query(F.data.startswith("run_"))
async def handle_run_test(call: types.CallbackQuery):
    if active_tests.get(call.message.chat.id):
        await call.answer("⚠️ Chatda faol test mavjud. Uni /stop orqali to'xtating.", show_alert=True)
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
    await call.message.edit_text(f"🚀 <b>{subject_name.replace('_', ' ')}: {chunk_index + 1}-bo'lim boshlandi!</b>\nSavollar: {len(chunk)} ta.")
    asyncio.create_task(send_test_chunk(call.message.chat.id, chunk, call.message.chat.type))

# --- TEST YUBORISH JARAYONI ---
async def send_test_chunk(chat_id: int, chunk: list, chat_type: str):
    active_tests[chat_id] = True 
    session_scores[chat_id] = {}
    
    async with aiosqlite.connect(DB_PATH) as db:
        for i, q in enumerate(chunk):
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
                logging.error(f"Xatolik: {e}")
                continue

            if chat_type == "private":
                event = asyncio.Event()
                private_chat_events[chat_id] = event
                try: await asyncio.wait_for(event.wait(), timeout=30.0)
                except asyncio.TimeoutError: pass  
                finally: private_chat_events.pop(chat_id, None)
                if not active_tests.get(chat_id):
                    try: await bot.stop_poll(chat_id=chat_id, message_id=msg.message_id)
                    except: pass
                    session_scores.pop(chat_id, None)
                    return
            else:
                for _ in range(30):
                    if not active_tests.get(chat_id):
                        try: await bot.stop_poll(chat_id=chat_id, message_id=msg.message_id)
                        except: pass
                        session_scores.pop(chat_id, None)
                        return
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
            results_text = "\n📊 Hech kim to'g'ri javob bermadi."

        await bot.send_message(chat_id, f"✅ Bo'lim tugadi!\n{results_text}\nℹ️ /leaderboard")
        active_tests.pop(chat_id, None)

@dp.message(Command("stop"))
async def stop_test_cmd(message: types.Message):
    chat_id = message.chat.id
    if active_tests.get(chat_id):
        active_tests[chat_id] = False
        if chat_id in private_chat_events: private_chat_events[chat_id].set()
        session_scores.pop(chat_id, None)
        await message.answer("🛑 <b>Test jarayoni to'xtatildi!</b>")
    else:
        await message.answer("⚠️ Faol test yo'q.")

@dp.message(Command("leaderboard"))
async def show_leaderboard(message: types.Message):
    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT full_name, score FROM scores WHERE chat_id = ? ORDER BY score DESC LIMIT 15", (chat_id,)) as cursor:
            users = await cursor.fetchall()
    if not users:
        await message.answer("📭 Hali reyting shakllanmagan.")
        return
    text = "<b>🏆 Umumiy Reyting:</b>\n\n"
    for i, (name, score) in enumerate(users, 1):
        text += f"{i}. {name} — {score} ball\n"
    await message.answer(text)

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
            if chat_id in private_chat_events: private_chat_events[chat_id].set()

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
