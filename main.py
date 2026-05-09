import os
import asyncio
import logging
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Tokenni Railway'dan olish
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "bot_db.sqlite"

# Faol testlarni nazorat qilish uchun lug'at
active_tests = {}

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

# --- 2. SAVOLLARNI O'QISH VA AJRATISH ---
def parse_questions(filename="questions.txt"):
    if not os.path.exists(filename):
        return []
    
    questions = []
    current_q = None
    
    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('?'):
                if current_q:
                    questions.append(current_q)
                current_q = {'question': line[1:].strip()[:300], 'options': [], 'correct_idx': 0}
                
            elif line.startswith('=='):
                current_q['correct_idx'] = len(current_q['options'])
                current_q['options'].append(line[2:].strip()[:100])
                
            elif line.startswith('='):
                current_q['options'].append(line[1:].strip()[:100])
                
    if current_q:
        questions.append(current_q)
        
    return questions

def chunk_questions(questions, size=30):
    return [questions[i:i + size] for i in range(0, len(questions), size)]

# --- 3. BUYRUQLAR (HANDLERS) ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    text = (
        "👋 Assalomu alaykum!\n\n"
        "Men yakuniy testlarni o'tkazib beruvchi botman. Meni guruhda yoki shu yerning o'zida ishlatishingiz mumkin.\n"
        "👉 Barcha buyruqlarni ko'rish uchun /help ni bosing."
    )
    await message.answer(text)

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    text = (
        "<b>🤖 Bot buyruqlari:</b>\n\n"
        "🔸 /start - Botni qayta ishga tushirish\n"
        "🔸 /help - Shu xabarni ko'rsatish\n"
        "🔸 /test - Savollar bo'limini ko'rish va testni boshlash\n"
        "🔸 /stop - Faol testni darhol to'xtatish\n"
        "🔸 /leaderboard - Hozirgi chatning reytingini ko'rish"
    )
    await message.answer(text)

@dp.message(Command("test"))
async def send_tests_menu(message: types.Message):
    questions = parse_questions("questions.txt")
    if not questions:
        await message.answer("⚠️ Savollar fayli bo'sh yoki topilmadi! (questions.txt faylini tekshiring)")
        return

    chunks = chunk_questions(questions, 30)
    
    # UI tugmalari (Apple-style toza dizayn)
    builder = InlineKeyboardBuilder()
    for i, chunk in enumerate(chunks):
        start_num = (i * 30) + 1
        end_num = start_num + len(chunk) - 1
        btn_text = f"{i + 1}-bo'lim ({start_num}-{end_num})"
        builder.button(text=btn_text, callback_data=f"start_test_{i}")
        
    builder.adjust(2)

    await message.answer(
        "📚 <b>Qaysi bo'limdan test yechishni xohlaysiz?</b>\nO'zingizga kerakli bo'limni tanlang:",
        reply_markup=builder.as_markup()
    )

@dp.message(Command("stop"))
async def stop_test_cmd(message: types.Message):
    chat_id = message.chat.id
    if active_tests.get(chat_id):
        active_tests[chat_id] = False
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

    text = "<b>🏆 Reyting:</b>\n\n"
    for i, (name, score) in enumerate(users, 1):
        text += f"{i}. {name} — {score} ball\n"
        
    await message.answer(text)

# --- 4. TEST YUBORISH JARAYONI (FON REJIMIDA) ---
async def send_test_chunk(chat_id: int, chunk: list):
    active_tests[chat_id] = True 
    
    async with aiosqlite.connect(DB_PATH) as db:
        for i, q in enumerate(chunk):
            # To'xtatilganligini tekshirish
            if not active_tests.get(chat_id):
                return
                
            if len(q['options']) < 2:
                continue
            
            try:
                msg = await bot.send_poll(
                    chat_id=chat_id,
                    question=q['question'],
                    options=q['options'],
                    type='quiz',
                    correct_option_id=q['correct_idx'],
                    is_anonymous=False
                )
                await db.execute("INSERT OR REPLACE INTO active_polls (poll_id, correct_option_id, chat_id) VALUES (?, ?, ?)", 
                                 (msg.poll.id, q['correct_idx'], chat_id))
                await db.commit()
            except Exception as e:
                logging.error(f"Poll yuborishda xatolik: {e}")

            # Eng oxirgi savol bo'lmasa, 30 soniya kutish
            if i < len(chunk) - 1:
                # Bot stop buyrug'iga tez reaksiya qilishi uchun 1 soniyadan kutamiz
                for _ in range(30):
                    if not active_tests.get(chat_id):
                        return
                    await asyncio.sleep(1)
                
    if active_tests.get(chat_id):
        await bot.send_message(chat_id, "✅ Tanlangan bo'limdagi barcha savollar yuborib bo'lindi!")
        active_tests.pop(chat_id, None)

@dp.callback_query(F.data.startswith("start_test_"))
async def handle_start_test(call: types.CallbackQuery):
    # Bir vaqtning o'zida bitta guruhda faqat bitta test ketishi kerak
    if active_tests.get(call.message.chat.id):
        await call.answer("⚠️ Bu chatda allaqachon test jarayoni ketmoqda. Oldin uni /stop orqali to'xtating.", show_alert=True)
        return

    chunk_index = int(call.data.split("_")[-1])
    questions = parse_questions("questions.txt")
    chunks = chunk_questions(questions, 30)
    
    if chunk_index >= len(chunks):
        await call.answer("Xatolik! Bu bo'lim topilmadi.", show_alert=True)
        return
        
    chunk = chunks[chunk_index]
    
    await call.message.edit_text(
        f"🚀 <b>{chunk_index + 1}-bo'lim boshlandi!</b>\n"
        f"Jami: {len(chunk)} ta savol.\n\n"
        f"<i>⏳ Har bir savol 30 soniya interval bilan yuboriladi...</i>\n"
        f"<i>🛑 To'xtatish uchun /stop buyrug'ini bering.</i>"
    )
    await call.answer()
    
    # Orqa fonga yuborish
    asyncio.create_task(send_test_chunk(call.message.chat.id, chunk))

# --- 5. JAVOBLARNI TEKSHIRISH VA BALL BERISH ---
@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    full_name = poll_answer.user.first_name
    if poll_answer.user.last_name:
        full_name += f" {poll_answer.user.last_name}"
    
    selected_option = poll_answer.option_ids[0]

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT correct_option_id, chat_id FROM active_polls WHERE poll_id = ?", (poll_id,)) as cursor:
            row = await cursor.fetchone()
            
        if row:
            correct_option, chat_id = row
            if selected_option == correct_option:
                await db.execute("""
                    INSERT INTO scores (user_id, chat_id, full_name, score) 
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(user_id, chat_id) 
                    DO UPDATE SET score = score + 1, full_name = excluded.full_name
                """, (user_id, chat_id, full_name))
                await db.commit()

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
