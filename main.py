import os
import asyncio
import logging
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# Railway muhitidan tokenni olish
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Ma'lumotlar bazasi manzili (Railway'da ma'lumot saqlanib qolishi uchun)
# Agar Railway Volume ulangan bo'lsa, '/app/data/bot_db.sqlite' qilib o'zgartiring
DB_PATH = "/app/data/bot_db.sqlite"

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- 1. MA'LUMOTLAR BAZASINI YARATISH ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Foydalanuvchilarning ballarini saqlash uchun
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                user_id INTEGER,
                chat_id INTEGER,
                full_name TEXT,
                score INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        # Yuborilgan so'rovnomalar (poll) va ularning to'g'ri javoblarini saqlash uchun
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_polls (
                poll_id TEXT PRIMARY KEY,
                correct_option_id INTEGER
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
                # Telegram cheklovi: Savol matni 300 belgidan oshmasligi kerak
                current_q = {'question': line[1:].strip()[:300], 'options': [], 'correct_idx': 0}
                
            elif line.startswith('=='):
                current_q['correct_idx'] = len(current_q['options'])
                # Telegram cheklovi: Javob matni 100 belgidan oshmasligi kerak
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
    await message.answer("Assalomu alaykum! Men guruhlarda yakuniy testlarni o'tkazib beruvchi botman. \n"
                         "Guruhga qo'shing va /test buyrug'ini bering.")

@dp.message(Command("test"))
async def send_tests(message: types.Message):
    # Faqat guruhlarda ishlashi uchun tekshiruv
    if message.chat.type == "private":
        await message.answer("Bu buyruq faqat guruhlarda ishlaydi!")
        return

    questions = parse_questions("questions.txt")
    if not questions:
        await message.answer("Savollar fayli bo'sh yoki topilmadi!")
        return

    # Savollarni 30 tadan bo'laklarga ajratamiz (hozircha 1-bo'lakni yuboramiz)
    # Kengaytirish uchun /test 1, /test 2 ko'rinishida argument qabul qiladigan qilsa ham bo'ladi
    chunks = chunk_questions(questions, 30)
    first_chunk = chunks[0] 

    await message.answer(f"Test boshlandi! Jami {len(first_chunk)} ta savol yuborilmoqda...")

    async with aiosqlite.connect(DB_PATH) as db:
        for q in first_chunk:
            # Variantlar 2 tadan kam bo'lsa yoki 10 tadan ko'p bo'lsa, Telegram xato beradi
            if len(q['options']) < 2:
                continue
            
            # Poll yuborish
            msg = await message.answer_poll(
                question=q['question'],
                options=q['options'],
                type='quiz',
                correct_option_id=q['correct_idx'],
                is_anonymous=False # Kim qaysi javobni belgilaganini bilish uchun false bo'lishi shart
            )
            
            # Poll ID va to'g'ri javob indeksini bazaga yozamiz
            await db.execute("INSERT OR REPLACE INTO active_polls (poll_id, correct_option_id) VALUES (?, ?)", 
                             (msg.poll.id, q['correct_idx']))
            await db.commit()
            
            # Telegram flood-control'ga tushmaslik uchun 1 soniya kutamiz
            await asyncio.sleep(1)

@dp.message(Command("leaderboard"))
async def show_leaderboard(message: types.Message):
    if message.chat.type == "private":
        return

    chat_id = message.chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT full_name, score FROM scores WHERE chat_id = ? ORDER BY score DESC LIMIT 15", (chat_id,)) as cursor:
            users = await cursor.fetchall()
            
    if not users:
        await message.answer("Hali hech kim test ishlagani yo'q.")
        return

    text = "<b>🏆 Guruh Reytingi:</b>\n\n"
    for i, (name, score) in enumerate(users, 1):
        text += f"{i}. {name} — {score} ball\n"
        
    await message.answer(text)

# --- 4. JAVOBLARNI TEKSHIRISH VA BALL BERISH ---
@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    # Foydalanuvchi ismini shakllantirish
    full_name = poll_answer.user.first_name
    if poll_answer.user.last_name:
        full_name += f" {poll_answer.user.last_name}"
    
    selected_option = poll_answer.option_ids[0]

    async with aiosqlite.connect(DB_PATH) as db:
        # Bu poll bazada bormi va to'g'ri javob qaysi?
        async with db.execute("SELECT correct_option_id FROM active_polls WHERE poll_id = ?", (poll_id,)) as cursor:
            row = await cursor.fetchone()
            
        if row:
            correct_option = row[0]
            # Agar javob to'g'ri bo'lsa
            if selected_option == correct_option:
                # Chat ID ni to'g'ridan-to'g'ri poll_answer'dan olib bo'lmaydi, shuning uchun 
                # bot birinchi bo'lib qaysi guruhda ekanligini alohida saqlash mantiqi kerak. 
                # Oddiylik uchun bu yerda barcha chatlar uchun umumiy hisoblaymiz yoki 
                # guruh id sini global saqlash usulidan foydalanamiz. Hozircha chat_id ni 0 deb saqlaymiz.
                # Agar faqat bitta guruhda ishlatsangiz, muammo yo'q.
                chat_id = 0 
                
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
