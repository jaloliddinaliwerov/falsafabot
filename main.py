import os
import re
import random  # Tasodifiy tanlash va aralashtirish uchun
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

# Faol testlarni nazorat qilish va faqat shu raunddagi ballarni hisoblash
active_tests = {}
session_scores = {}

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

# --- 2. YANGI CHIZIQLI FORMATDAGI SAVOLLARNI O'QISH ---
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
                
            # Savol belgisi (++++)
            if line.startswith('++++'):
                if current_q and len(current_q['options']) >= 2:
                    questions.append(current_q)
                current_q = {'question': '', 'options': [], 'correct_idx': 0}
                expecting_question_text = True
                expecting_option_text = False
                continue
                
            # Variant belgisi (====)
            if line.startswith('===='):
                expecting_question_text = False
                expecting_option_text = True
                continue
                
            # Savol matnini o'qish
            if expecting_question_text and current_q:
                current_q['question'] = line[:300]
                expecting_question_text = False
                continue
                
            # Variant matnini o'qish va to'g'ri javobni (#) aniqlash
            if expecting_option_text and current_q:
                if line.startswith('#'):
                    current_q['correct_idx'] = len(current_q['options'])
                    option_text = line[1:].strip()  # # belgisini olib tashlaymiz
                else:
                    option_text = line
                current_q['options'].append(option_text[:100])
                expecting_option_text = False
                continue
                
    if current_q and len(current_q['options']) >= 2:
        questions.append(current_q)
        
    return questions

# --- 3. BUYRUQLAR (HANDLERS) ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    text = (
        "👋 Assalomu alaykum!\n\n"
        "Men test o'tkazib beruvchi aqlli botman. Savollar va variantlar avtomatik aralashtiriladi.\n"
        "👉 Testni boshlash uchun /test buyrug'ini bosing."
    )
    await message.answer(text)

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    text = (
        "<b>🤖 Bot buyruqlari:</b>\n\n"
        "🔸 /start - Botni qayta ishga tushirish\n"
        "🔸 /test - 1000 ta savoldan tasodifiy 30 tasini tanlab testni boshlash\n"
        "🔸 /stop - Faol testni darhol to'xtatish\n"
        "🔸 /leaderboard - Umumiy chat reytingini ko'rish"
    )
    await message.answer(text)

@dp.message(Command("test"))
async def start_direct_test(message: types.Message):
    chat_id = message.chat.id
    if active_tests.get(chat_id):
        await message.answer("⚠️ Bu chatda allaqachon test jarayoni ketmoqda. Oldin uni /stop orqali to'xtating.")
        return

    questions = parse_questions("questions.txt")
    if not questions:
        await message.answer("⚠️ Savollar fayli bo'sh yoki topilmadi (questions.txt)!")
        return

    # 1000 ta savol ichidan tasodifiy 30 tasini tanlab olamiz
    sample_size = min(30, len(questions))
    selected_chunk = random.sample(questions, sample_size)
    
    await message.answer(
        f"🚀 <b>Yangi test jarayoni boshlandi!</b>\n"
        f"Jami savollar ichidan <b>{sample_size} ta</b> tasodifiy test tanlab olindi.\n\n"
        f"<i>⏳ Har bir savolga 30 soniya vaqt beriladi va avtomatik yopiladi.</i>\n"
        f"<i>🛑 To'xtatish uchun /stop buyrug'ini bering.</i>"
    )
    
    asyncio.create_task(send_test_chunk(chat_id, selected_chunk))

@dp.message(Command("stop"))
async def stop_test_cmd(message: types.Message):
    chat_id = message.chat.id
    if active_tests.get(chat_id):
        active_tests[chat_id] = False
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

# --- 4. TEST YUBORISH VA VARIANTLARNI ARALASHTIRISH (SHUFFLE) ---
async def send_test_chunk(chat_id: int, chunk: list):
    active_tests[chat_id] = True 
    session_scores[chat_id] = {}  # Yangi raund uchun ballarni tozalaymiz
    
    async with aiosqlite.connect(DB_PATH) as db:
        for i, q in enumerate(chunk):
            if not active_tests.get(chat_id):
                session_scores.pop(chat_id, None)
                return
                
            if len(q['options']) < 2:
                continue
            
            # --- JAVOBLAR JOYINI TASODIFIY ALMASHTIRISH ---
            indexed_options = list(enumerate(q['options']))
            random.shuffle(indexed_options)
            shuffled_options = [opt for idx, opt in indexed_options]
            # Yangi ro'yxat ichidan to'g'ri javob qayerga o'tib qolganini aniqlaymiz
            correct_idx = next(new_i for new_i, (old_idx, opt) in enumerate(indexed_options) if old_idx == q['correct_idx'])
            
            try:
                msg = await bot.send_poll(
                    chat_id=chat_id,
                    question=q['question'],
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

            # 30 soniya kutish
            for _ in range(30):
                if not active_tests.get(chat_id):
                    try: await bot.stop_poll(chat_id=chat_id, message_id=msg.message_id)
                    except: pass
                    session_scores.pop(chat_id, None)
                    return
                await asyncio.sleep(1)
            
            # Poll'ni yopish
            try:
                await bot.stop_poll(chat_id=chat_id, message_id=msg.message_id)
            except Exception as e:
                logging.error(f"Poll yopishda xatolik: {e}")
                
    # --- 5. RAUND YAKUNI: NATIJALAR VA MOTIVATSIYA ---
    if active_tests.get(chat_id):
        current_results = session_scores.pop(chat_id, None)
        results_text = ""
        motivation_text = ""
        
        # Har xil motivatsion chiroyli gaplar ro'yxati
        motivational_quotes = [
            "✨ Barakalla, <b>{name}</b>! Bilimingizga ko'z tegmasin. Doimo shunday yuqori cho'qqilarni zabt eting! 🚀",
            "🌟 Ajoyib natija, <b>{name}</b>! Izlanishdan aslo to'xtamang, kelajakda bundanda ulkan muvaffaqiyatlar sizni kutmoqda!",
            "💪 Ofarin, <b>{name}</b>! Intilishingiz va mehnatingiz tahsinga loyiq. Harakatda barakat bor!",
            "🎓 Ilmingiz ziyoda bo'lsin, <b>{name}</b>! Bugungi ishtirokingiz judayam yuqori darajada bo'ldi, g'alaba muborak!",
            "❤️ Haqiqiy chempion, <b>{name}</b>! O'qish va o'rganishdan sira charchamang. Siz bilan faxrlanamiz!"
        ]
        
        if current_results:
            sorted_results = sorted(current_results.values(), key=lambda x: x['score'], reverse=True)
            results_text = "\n📊 <b>Ushbu raund natijalari:</b>\n\n"
            for idx, res in enumerate(sorted_results, 1):
                results_text += f"{idx}. {res['name']} — {res['score']} ta to'g'ri\n"
            
            # Eng yuqori ball olgan odamning ismini olamiz (Masalan: Madinaxon)
            top_user_name = sorted_results[0]['name']
            motivation_text = "\n" + random.choice(motivational_quotes).format(name=top_user_name)
        else:
            results_text = "\n📊 Afsuski, bu raundda hech kim to'g'ri javob bermadi."
            motivation_text = "\nHechqisi yo'q, keyingi safar albatta o'xshaydi! Kitob o'qishdan to'xtamang! 📚"

        await bot.send_message(chat_id, f"✅ <b>Test yakunlandi!</b>{results_text}{motivation_text}\n\nℹ️ Umumiy reyting: /leaderboard")
        active_tests.pop(chat_id, None)

# --- 6. JAVOBLARNI TEKSHIRISH VA JOVOB BERGANLARNI SESSYAGA YOZISH ---
@dp.poll_answer()
async def handle_poll_answer(poll_answer: types.PollAnswer):
    poll_id = poll_answer.poll_id
    user_id = poll_answer.user.id
    full_name = poll_answer.user.first_name
    if poll_answer.user.last_name:
        full_name += f" {poll_answer.user.last_name}"
    
    if not poll_answer.option_ids:
        return
    selected_option = poll_answer.option_ids[0]

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT correct_option_id, chat_id FROM active_polls WHERE poll_id = ?", (poll_id,)) as cursor:
            row = await cursor.fetchone()
            
        if row:
            correct_option, chat_id = row
            if selected_option == correct_option:
                # Umumiy ma'lumotlar bazasiga ball qo'shish
                await db.execute("""
                    INSERT INTO scores (user_id, chat_id, full_name, score) 
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(user_id, chat_id) 
                    DO UPDATE SET score = score + 1, full_name = excluded.full_name
                """, (user_id, chat_id, full_name))
                await db.commit()
                
                # Aynan shu o'yin sessiyasida ishtirok etayotgan foydalanuvchiga ball qo'shish
                if chat_id in session_scores:
                    if user_id not in session_scores[chat_id]:
                        session_scores[chat_id][user_id] = {"name": full_name, "score": 0}
                    session_scores[chat_id][user_id]["score"] += 1

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
