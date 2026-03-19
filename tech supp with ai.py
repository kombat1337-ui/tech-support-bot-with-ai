from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.enums import ChatType, ContentType
import asyncio
import logging
import aiohttp
import sqlite3
from typing import Dict, List, Optional
import difflib
from bs4 import BeautifulSoup

BOT_TOKEN = " token here " 
GROUP_CHAT_ID = -000000000  # swap on ur ID group
GEMINI_API_KEY = "0000000000000"  # put ur Gemini API
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"

# Соответствие: message_id из группы → user_id
message_link: Dict[int, int] = {}

# Хранилище материалов для контекста (временное, в памяти)
user_materials: Dict[int, List[str]] = {}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализация SQLite базы данных
def init_db():
    conn = sqlite3.connect("errors.db")
    cursor = conn.cursor()
    # Таблица для ошибок
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_text TEXT NOT NULL,
            solution_text TEXT NOT NULL
        )
    """)
    # Таблица для сайтов
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS websites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            keywords TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_user_label(user):
    return f"@{user.username}" if user.username else f"{user.full_name} (ID: {user.id})"

# Поиск похожей ошибки в базе
def find_similar_error(error_text: str) -> Optional[tuple[str, str]]:
    conn = sqlite3.connect("errors.db")
    cursor = conn.cursor()
    cursor.execute("SELECT error_text, solution_text FROM errors")
    errors = cursor.fetchall()
    conn.close()

    for stored_error, solution in errors:
        similarity = difflib.SequenceMatcher(None, error_text.lower(), stored_error.lower()).ratio()
        if similarity > 0.8:
            return stored_error, solution
    return None

# Поиск релевантного сайта в базе
def find_relevant_website(query: str) -> Optional[tuple[str, str]]:
    conn = sqlite3.connect("errors.db")
    cursor = conn.cursor()
    cursor.execute("SELECT url, keywords FROM websites")
    websites = cursor.fetchall()
    conn.close()

    for url, keywords in websites:
        keyword_list = keywords.lower().split(",")
        query_words = query.lower().split()
        if any(keyword.strip() in query_words for keyword in keyword_list):
            return url, keywords
    return None

# Добавление ошибки и решения в базу
def add_error_to_db(error_text: str, solution_text: str):
    conn = sqlite3.connect("errors.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO errors (error_text, solution_text) VALUES (?, ?)", (error_text, solution_text))
    conn.commit()
    conn.close()

# Добавление сайта в базу
def add_website_to_db(url: str, keywords: str):
    conn = sqlite3.connect("errors.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO websites (url, keywords) VALUES (?, ?)", (url, keywords))
    conn.commit()
    conn.close()

# Извлечение текста с сайта
async def fetch_website_content(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status == 200:
                    html = await response.text()
                    soup = BeautifulSoup(html, "html.parser")
                    # Извлекаем текст из тегов <p>, <h1>, <h2>, <h3>
                    text = " ".join([p.get_text(strip=True) for p in soup.find_all(["p", "h1", "h2", "h3"])])
                    return text[:2000]  # Ограничиваем длину для контекста
                else:
                    return f"Ошибка при загрузке сайта: {response.status}"
    except Exception as e:
        logging.error(f"Ошибка при загрузке сайта {url}: {e}")
        return f"Не удалось загрузить сайт: {e}"

# Запрос к Gemini API
async def query_gemini(user_id: int, query: str, website_content: str = "") -> str:
    headers = {
        "Content-Type": "application/json"
    }
    context = "\n".join(user_materials.get(user_id, [])) if user_id in user_materials else ""
    prompt = f"Контекст:\n{context}\n\nВеб-контент:\n{website_content}\n\nЗапрос пользователя: {query}\n\nОтветь кратко и по делу."

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 500,
            "temperature": 0.7
        }
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{GEMINI_API_URL}?key={GEMINI_API_KEY}", json=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "Ошибка: ответ от ИИ не получен")
                else:
                    error_text = await response.text()
                    logging.error(f"Ошибка API: {response.status} - {error_text}")
                    if response.status == 401:
                        return "Ошибка: Неверный API-ключ. Пожалуйста, свяжитесь с администратором бота."
                    return f"Ошибка API: {response.status} - {error_text}"
    except Exception as e:
        logging.error(f"Ошибка при запросе к API: {e}")
        return "Ошибка: Не удалось связаться с сервером ИИ."

# Обработка команды /ai
@dp.message(F.chat.type == ChatType.PRIVATE, F.text.startswith("/ai"))
async def handle_ai_command(message: Message):
    user = message.from_user
    query = message.text[4:].strip()
    if not query:
        await message.answer("Пожалуйста, укажите запрос после /ai, например: /ai Ошибка: NameError: name 'x' is not defined")
        return

    header = f"🤖 Запрос к ИИ от {get_user_label(user)}: {query}"
    sent = await bot.send_message(GROUP_CHAT_ID, header)
    message_link[sent.message_id] = user.id

    # Проверка базы данных на похожие ошибки
    similar_error = find_similar_error(query)
    if similar_error:
        error_text, solution = similar_error
        response = f"Найдено похожее решение:\nОшибка: {error_text}\nРешение: {solution}"
    else:
        # Проверка базы на релевантные сайты
        website = find_relevant_website(query)
        website_content = ""
        if website:
            url, keywords = website
            website_content = await fetch_website_content(url)
            response = await query_gemini(user.id, query, website_content)
        else:
            response = await query_gemini(user.id, query)

    await bot.send_message(user.id, f"💬 Ответ от ИИ:\n\n{response}")
    await bot.send_message(GROUP_CHAT_ID, f"💬 Ответ ИИ для {get_user_label(user)}:\n\n{response}")

# Обработка команды /add_website
@dp.message(F.chat.type == ChatType.PRIVATE, F.text.startswith("/add_website"))
async def handle_add_website(message: Message):
    user = message.from_user
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Пожалуйста, укажите URL и ключевые слова, например: /add_website https://example.com python,errors,nameerror")
        return

    url, keywords = parts[1], parts[2]
    try:
        # Проверяем, что URL валидный
        async with aiohttp.ClientSession() as session:
            async with session.head(url, timeout=5) as response:
                if response.status != 200:
                    await message.answer(f"Ошибка: URL {url} недоступен (статус {response.status})")
                    return
        add_website_to_db(url, keywords)
        await message.answer(f"Сайт {url} с ключевыми словами '{keywords}' добавлен в базу.")
    except Exception as e:
        await message.answer(f"Ошибка при добавлении сайта: {e}")

# Обработка загрузки материалов
@dp.message(F.chat.type == ChatType.PRIVATE, F.content_type == ContentType.DOCUMENT)
async def handle_document(message: Message):
    user = message.from_user
    document = message.document

    if document.mime_type.startswith("text"):
        file = await bot.get_file(document.file_id)
        file_content = await bot.download_file(file.file_path)
        text = file_content.read().decode("utf-8", errors="ignore")

        if user.id not in user_materials:
            user_materials[user.id] = []
        user_materials[user.id].append(text)

        header = f"📄 Материал от {get_user_label(user)}"
        sent = await bot.send_document(GROUP_CHAT_ID, document=document.file_id, caption=header)
        message_link[sent.message_id] = user.id

        await message.answer("Материал успешно загружен и будет использован для ответов ИИ.")
    else:
        await message.answer("Пожалуйста, загрузите текстовый файл.")

# Обработка сообщений в ЛС
@dp.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_message(message: Message):
    user = message.from_user
    header = f"📨 Сообщение от {get_user_label(user)}:"

    sent = None
    if message.text and not message.text.startswith(("/ai", "/add_website")):
        sent = await bot.send_message(GROUP_CHAT_ID, f"{header}\n\n{message.text}")
    elif message.photo:
        sent = await bot.send_photo(GROUP_CHAT_ID, photo=message.photo[-1].file_id, caption=header)
    elif message.video:
        sent = await bot.send_video(GROUP_CHAT_ID, video=message.video.file_id, caption=header)
    elif message.audio:
        sent = await bot.send_audio(GROUP_CHAT_ID, audio=message.audio.file_id, caption=header)
    elif message.voice:
        sent = await bot.send_voice(GROUP_CHAT_ID, voice=message.voice.file_id, caption=header)

    if sent:
        message_link[sent.message_id] = user.id

# Обработка ответов из группы
@dp.message(F.chat.id == GROUP_CHAT_ID)
async def handle_group_reply(message: Message):
    if not message.reply_to_message:
        return

    replied_id = message.reply_to_message.message_id
    user_id = message_link.get(replied_id)
    if not user_id:
        return

    try:
        if message.text and message.reply_to_message.text:
            error_text = message.reply_to_message.text.split(":", 1)[-1].strip()
            add_error_to_db(error_text, message.text)

        if message.text:
            await bot.send_message(user_id, f"💬 Ответ от Технической поддержки:\n\n{message.text}")
        elif message.photo:
            await bot.send_photo(user_id, photo=message.photo[-1].file_id, caption="💬 Ответ от Технической поддержки")
        elif message.video:
            await bot.send_video(user_id, video=message.video.file_id, caption="💬 Ответ от Технической поддержки")
        elif message.voice:
            await bot.send_voice(user_id, voice=message.voice.file_id, caption="💬 Ответ от Технической поддержки")
        elif message.audio:
            await bot.send_audio(user_id, audio=message.audio.file_id, caption="💬 Ответ от Технической поддержки")
        elif message.document:
            await bot.send_document(user_id, document=message.document.file_id, caption="💬 Ответ от Технической поддержки")
    except Exception as e:
        logging.error(f"Ошибка при отправке пользователю: {e}")

# Запуск
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())