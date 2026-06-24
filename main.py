import os
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

# Faol testlarni, ballarni va tezkor o'tish hodisalarini nazorat qilish
active_tests = {}
session_scores = {}
chat_events = {}  # Javob berilganda taymerni buzib keyingi savolga o'tkazish uchun

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- 1. MA'LUMOTLAR BAZASINI YARATISH ---
async def init_db():
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

# --- 2. CHIZIQLI FORMATDAGI SAVOLLARNI O'QISH (PARSER) ---
def parse_questions(filename="questions.txt"):
    if not os.path.exists(filename):
        return []
    
    questions = []
    current_q = None
    
    expecting_question_text = False
    expecting_option_text = False
    
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('++++'):
                if current_q and len(current_q['options']) >= 2:
                    questions.append(current_q)
                current_q = {'question': '', 'options': [], 'correct_idx': 0}
                expecting_question_text = True
                expecting_option_text = False
                continue
                
            if line.startswith('===='):
                expecting_question_text = False
                expecting_option_text = True
                continue
                
            if expecting_question_text and current_q:
                current_q['question'] = line[:300]
                expecting_question_text = False
                continue
                
            if expecting_option_text and current_q:
                if line.startswith('#'):
                    current_q['correct_idx'] = len(current_q['options'])
                    option_text = line[1:].strip()
                else:
                    option_text = line
                current_q['options'].append(option_text[:100])
                expecting_option_text = False
                continue
                
    if current_q and len(current_q['options']) >= 2:
        questions.append(current_q)
        
    return questions

def chunk_questions(questions, size=30):
    return [questions[i:i + size] for i in range(0, len(questions), size)]

# --- 3. BUYRUQLAR (HANDLERS) ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await message.answer("👋 Assalomu alaykum!\n\nJavob berilganda darhol keyingi savolga o'tadigan aqlli test botiga xush kelibsiz.\n👉 Bo'limlarni ko'rish uchun /test buyrug'ini bosing.")

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    text = (
        "<b>🤖 Bot buyruqlari:</b>\n\n"
        "🔸 /test - Bo'limlarni ko'rish va testni boshlash\n"
        "🔸 /stop - Ketayotgan testni darhol to'xtatish\n"
        "🔸 /leaderboard - Umumiy guruh reytingini ko'rish"
    )
    await message.answer(text)

@dp.message(Command("test"))
async def send_tests_menu(message: types.Message):
    chat_id = message.chat.id
    if active_tests.get(chat_id):
        await message.answer("⚠️ Bu chatda allaqachon test jarayoni ketmoqda. Oldin uni /stop orqali to'xtating.")
        return

    questions = parse_questions("questions.txt")
    if not questions:
        await message.answer("⚠️ Savollar fayli bo'sh yoki topilmadi (questions.txt)!")
        return

    chunks = chunk_questions(questions, 30)
    builder = InlineKeyboardBuilder()
    for i, chunk in enumerate(chunks):
        start_num = (i * 30) + 1
        end_num = start_num + len(chunk) - 1
        builder.button(text=f"📦 {i + 1}-bo'lim ({start_num}-{end_num})", callback_data=f"start_test_{i}")
        
    builder.adjust(2)
    await message.answer(f"📚 <b>Jami {len(questions)} ta ketma-ket savol bor.</b>\nBo'limni tanlang:", reply_markup=builder.as_markup())

@dp.message(Command("stop"))
async def stop_test_cmd(message: types.Message):
    chat_id = message.chat.id
    if active_tests.get(chat_id):
        active_tests[chat_id] = False
        if chat_id in chat_events:
            chat_events[chat_id].set()  # Kutish jarayonini darhol buzish
        session_scores.pop(chat_id, None)
        await message.answer("🛑 <b>Test jarayoni darhol to'xtatildi!</b>")
    else:
        await message.answer("⚠️ Hozircha faol test jarayoni yo'q.")

@dp.message(Command("leaderboard"))
async def show_leaderboard(message: types.Message):
    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT full_name, score FROM scores WHERE chat_id = ? ORDER BY score DESC LIMIT 15", (chat_id,)) as cursor:
            users = await cursor.fetchall()
    if not users:
        await message.answer("📭 Hali hech kim test ishlagani yo'q.")
        return
    text = "<b>🏆 Umumiy Reyting (Top 15):</b>\n\n"
    for i, (name, score) in enumerate(users, 1):
        text += f"{i}. {name} — {score} ball\n"
    await message.answer(text)

# --- 4. CALLBACK ORQALI BO'LIMNI ISHGA TUSHIRISH ---
@dp.callback_query(F.data.startswith("start_test_"))
async def handle_start_test(call: types.CallbackQuery):
    chat_id = call.message.chat.id
    if active_tests.get(chat_id):
        await call.answer("⚠️ Chatda faol test jarayoni ketmoqda.", show_alert=True)
        return

    chunk_index = int(call.data.split("_")[-1])
    questions = parse_questions("questions.txt")
    chunks = chunk_questions(questions, 30)
    
    if chunk_index >= len(chunks):
        await call.answer("Bo'lim topilmadi.", show_alert=True)
        return
        
    chunk = chunks[chunk_index]
    await call.message.edit_text(
        f"🚀 <b>{chunk_index + 1}-bo'lim boshlandi!</b>\n"
        f"Savollar: <b>{(chunk_index * 30) + 1} - {(chunk_index * 30) + len(chunk)}</b>\n\n"
        f"<i>💡 Kimdir belgilasa yoki 30 soniya o'tsa, keyingi savolga tezkor o'tadi!</i>"
    )
    await call.answer()
    
    asyncio.create_task(send_test_chunk(chat_id, chunk, chunk_index + 1))

# --- 5. TEST JONLI JARAYONI (TEZKOR O'TISH TIZIMI BILAN) ---
async def send_test_chunk(chat_id: int, chunk: list, section_num: int):
    active_tests[chat_id] = True 
    session_scores[chat_id] = {}
    chat_events[chat_id] = asyncio.Event()  # Har bir chat uchun event yaratamiz
    
    async with aiosqlite.connect(DB_PATH) as db:
        for i, q in enumerate(chunk):
            if not active_tests.get(chat_id):
                session_scores.pop(chat_id, None)
                chat_events.pop(chat_id, None)
                return
                
            if len(q['options']) < 2:
                continue
            
            # Eventni yangi savol uchun tozalaymiz
            chat_events[chat_id].clear()
            
            # Variantlarni tasodifiy aralashtirish (Shuffle)
            indexed_options = list(enumerate(q['options']))
            random.shuffle(indexed_options)
            shuffled_options = [opt for idx, opt in indexed_options]
            correct_idx = next(new_i for new_i, (old_idx, opt) in enumerate(indexed_options) if old_idx == q['correct_idx'])
            
            try:
                msg = await bot.send_poll(
                    chat_id=chat_id,
                    question=f"❓ Savol [{i+1}/{len(chunk)}]:\n{q['question']}",
                    options=shuffled_options,
                    type='quiz',
                    correct_option_id=correct_idx,
                    is_anonymous=False
                )
                await db.execute("INSERT OR REPLACE INTO active_polls (poll_id, correct_option_id, chat_id) VALUES (?, ?, ?)", 
                                 (msg.poll.id, correct_idx, chat_id))
                await db.commit()
            except Exception as e:
                logging.error(f"Poll yuborishda xatolik: {e}")
                continue

            # 🔥 ENGLIK: 30 soniya kutadi yoki javob berilishi bilan (event.set() bo'lganda) loop darhol sinadi!
            try:
                await asyncio.wait_for(chat_events[chat_id].wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass  # Hech kim javob bermasa 30 soniyada o'zi yopiladi
            
            # Poll'ni to'xtatish va keyingi savolga o'tish
            try:
                await bot.stop_poll(chat_id=chat_id, message_id=msg.message_id)
            except:
                pass
            
            await asyncio.sleep(0.5)  # Savollar orasida birozgina uzilish (Telegram bloklamasligi uchun)

    # --- 6. NATIJALAR VA ISMLI MOTIVATSIYA ---
    if active_tests.get(chat_id):
        current_results = session_scores.pop(chat_id, None)
        chat_events.pop(chat_id, None)
        results_text = ""
        motivation_text = ""
        
        motivational_quotes = [
            "✨ Barakalla, <b>{name}</b>! Bilimingizga ko'z tegmasin. Doimo shunday yuqori cho'qqilarni zabt eting! 🚀",
            "🌟 Ajoyib natija, <b>{name}</b>! Izlanishdan aslo to'xtamang, kelajakda ulkan muvaffaqiyatlar sizni kutmoqda!",
            "💪 Ofarin, <b>{name}</b>! Intilishingiz va mehnatingiz tahsinga loyiq. Harakatda barakat bor!",
            "🎓 Ilmingiz ziyoda bo'lsin, <b>{name}</b>! Bugungi ishtirokingiz judayam yuqori darajada bo'ldi, g'alaba muborak!",
            "❤️ Haqiqiy chempion, <b>{name}</b>! O'qish va o'rganishdan sira charchamang. Siz bilan faxrlanamiz!"
        ]
        
        if current_results:
            sorted_results = sorted(current_results.values(), key=lambda x: x['score'], reverse=True)
            results_text = f"\n📊 <b>{section_num}-bo'lim natijalari:</b>\n\n"
            for idx, res in enumerate(sorted_results, 1):
                results_text += f"{idx}. {res['name']} — {res['score']} ta to'g'ri\n"
            
            # G'olib ismini joylashtirish (Masalan: Madinaxon)
            top_user_name = sorted_results[0]['name']
            motivation_text = "\n" + random.choice(motivational_quotes).format(name=top_user_name)
        else:
            results_text = f"\n📊 Afsuski, {section_num}-bo'limda hech kim to'g'ri javob bermadi."
            motivation_text = "\nHechqisi yo'q, keyingi bo'limda albatta o'xshaydi! O'qishdan to'xtamang! 📚"

        await bot.send_message(chat_id, f"✅ <b>{section_num}-bo'lim yakunlandi!</b>{results_text}{motivation_text}")
        active_tests.pop(chat_id, None)

# --- 7. JAVOBLARNI TEKSHIRISH VA TEZKOR SIGNAL YUBORISH ---
@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    full_name = poll_answer.user.first_name + (f" {poll_answer.user.last_name}" if poll_answer.user.last_name else "")
    
    if not poll_answer.option_ids:
        return
    selected_option = poll_answer.option_ids[0]

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT correct_option_id, chat_id FROM active_polls WHERE poll_id = ?", (poll_id,)) as cursor:
            row = await cursor.fetchone()
            
        if row:
            correct_option, chat_id = row
            if selected_option == correct_option:
                # 1. Bazaga saqlash
                await db.execute("""
                    INSERT INTO scores (user_id, chat_id, full_name, score) 
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(user_id, chat_id) 
                    DO UPDATE SET score = score + 1, full_name = excluded.full_name
                """, (user_id, chat_id, full_name))
                await db.commit()
                
                # 2. Joriy sessiya ballini yangilash
                if chat_id in session_scores:
                    if user_id not in session_scores[chat_id]:
                        session_scores[chat_id][user_id] = {"name": full_name, "score": 0}
                    session_scores[chat_id][user_id]["score"] += 1

            # 🔥 JAVOB BERILDI: Taymerni to'xtatib, keyingi savolga o'tish signalini yoqamiz
            if chat_id in chat_events:
                chat_events[chat_id].set()

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
