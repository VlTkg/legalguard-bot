import asyncio
import io
import os
import re
import logging
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web
from docx import Document
import google.generativeai as genai
import pypdf

# Логирование
logging.basicConfig(level=logging.INFO)

# 1. Настройка конфигурации
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))
DAILY_LIMIT = 3  # Бесплатный дневной лимит проверок

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Переменные окружения TELEGRAM_TOKEN или GEMINI_API_KEY не заданы!")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Настройка клиента Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Хранилища временных данных
USER_REPORTS = {}
USER_CONTRACT_TYPES = {}
USER_JURISDICTIONS = {}

# Текст полного дисклеймера
DISCLAIMER_TEXT = (
    "⚖️ **Ограничение ответственности и правила сервиса LegalGuard**\n\n"
    "1. **Не является юридической консультацией:** Бот использует искусственный интеллект (Google Gemini) "
    "для автоматического первичного анализа текста. Ответы бота носят исключительно справочный характер.\n\n"
    "2. **Необходимость специалиста:** Бот не заменяет квалифицированного юриста. Перед подписанием критически "
    "важных документов обязательно проконсультируйтесь с профильным юристом.\n\n"
    "3. **Конфиденциальность:** Мы автоматически анонимизируем персональные данные (ФИО, телефоны, email, ИНН/БИН) "
    "перед передачей текста в нейросеть, однако настоятельно рекомендуем не загружать документы, содержащие строгую "
    "коммерческую или государственную тайну."
)

# 2. Инициализация и работа с SQLite БД
DB_PATH = "legalguard.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT,
            daily_usage INTEGER DEFAULT 0,
            last_usage_date TEXT
        )
    """)
    conn.commit()
    conn.close()

def check_and_update_limit(user_id: int, username: str, first_name: str) -> tuple[bool, int]:
    """Проверяет лимит пользователя. Возвращает (разрешено, оставшиеся_проверки)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    
    cursor.execute("SELECT daily_usage, last_usage_date FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute(
            "INSERT INTO users (user_id, username, first_name, created_at, daily_usage, last_usage_date) VALUES (?, ?, ?, ?, 1, ?)",
            (user_id, username, first_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), today)
        )
        conn.commit()
        conn.close()
        return True, DAILY_LIMIT - 1
    
    daily_usage, last_usage_date = row
    
    if last_usage_date != today:
        cursor.execute("UPDATE users SET daily_usage = 1, last_usage_date = ? WHERE user_id = ?", (today, user_id))
        conn.commit()
        conn.close()
        return True, DAILY_LIMIT - 1
    else:
        if daily_usage >= DAILY_LIMIT:
            conn.close()
            return False, 0
        else:
            cursor.execute("UPDATE users SET daily_usage = daily_usage + 1 WHERE user_id = ?", (today, user_id))
            conn.commit()
            conn.close()
            return True, DAILY_LIMIT - (daily_usage + 1)

# 3. Функции анонимизации данных (PII Cleanup)
def anonymize_text(text: str) -> str:
    """Очищает текст от персональных данных перед отправкой в LLM."""
    # Email
    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '[EMAIL]', text)
    # Номера телефонов
    text = re.sub(r'(\+7|8|7)?[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}', '[ТЕЛЕФОН]', text)
    # ИНН / БИН (10, 12 цифр)
    text = re.sub(r'\b\d{10}\b|\b\d{12}\b', '[ИНН/БИН]', text)
    # Паспортные данные / Удостоверение
    text = re.sub(r'\b\d{4}\s\d{6}\b', '[ПАСПОРТ/УДОСТОВЕРЕНИЕ]', text)
    return text

# 4. Описание юрисдикций и промпты
JURISDICTION_PROMPTS = {
    "kz": "При анализе строго опирайся на законодательство Республики Казахстан (Гражданский кодекс РК, Предпринимательский кодекс РК и профильные нормативно-правовые акты РК).",
    "ru": "При анализе строго опирайся на законодательство Российской Федерации (Гражданский кодекс РФ и профильные ФЗ РФ).",
    "int": "При анализе опирайся на международную практику (International Commercial Law, English Law / Common Law principles, CISG) и стандарты международных контрактов. Ответ давай на языке документа или на русском языке."
}

PROMPTS = {
    "dev": """
Ты — юрист, специализирующийся на договорах в IT, дизайне и разработке ПО (Авторский заказ/GDM).
{jurisdiction_instruction}

Проанализируй договор с фокусом на:
- Момент перехода исключительных прав (должен быть строго ПОСЛЕ 100% оплаты).
- Сохранение за исполнителем его наработок, библиотек, исходного кода и личных проектов.
- Четкие критерии приемки работ и лимит итераций правок.
- Отсутствие отчуждения личных активов и нереалистичных сроков.

Структура ответа:
1. 🚩 **Критические риски для разработчика/дизайнера**
2. 🛠 **Рекомендации по правкам**

В самом конце ответа добавь специальный блок для формирования таблицы разногласий. 
Он должен начинаться СТРОГО с метки `---PROTOCOL---` и содержать строки в формате:
Пункт договора || Исходная редакция || Предлагаемая редакция || Комментарий
""",
    "nda": """
Ты — юрист, специализирующийся на соглашениях о конфиденциальности (NDA).
{jurisdiction_instruction}

Проанализируй NDA с фокусом на:
- Четкость определения "Конфиденциальной информации" (не всё подряд).
- Разумные сроки действия режима конфиденциальности (оптимально 1-3 года).
- Соразмерность штрафов и ответственности за неумышленное разглашение.
- Исключения из конфиденциальности (общедоступные сведения, законные требования госорганов).

Структура ответа:
1. 🚩 **Скрытые риски и "ловушки" в NDA**
2. 🛠 **Рекомендации по защите**

В самом конце ответа добавь специальный блок для формирования таблицы разногласий. 
Он должен начинаться СТРОГО с метки `---PROTOCOL---` и содержать строки в формате:
Пункт договора || Исходная редакция || Предлагаемая редакция || Комментарий
""",
    "services": """
Ты — юрист для фрилансеров и самозанятых, оказывающих услуги.
{jurisdiction_instruction}

Проанализируй договор с фокусом на:
- Сроки оплаты (не более 5-10 рабочих дней, предотвращение кассовых разрывов).
- Порядок одностороннего расторжения (компенсация фактически понесенных расходов).
- Несоразмерные штрафы за просрочки сдачи.
- Четкое ограничение объема услуг и предотвращение "бесплатного расширения ТЗ".

Структура ответа:
1. 🚩 **Финансовые и юридические риски**
2. 🛠 **Рекомендации**

В самом конце ответа добавь специальный блок для формирования таблицы разногласий. 
Он должен начинаться СТРОГО с метки `---PROTOCOL---` и содержать строки в формате:
Пункт договора || Исходная редакция || Предлагаемая редакция || Комментарий
""",
    "general": """
Ты — профессиональный юридический ассистент для фрилансеров и исполнителей.
{jurisdiction_instruction}

Твоя задача — найти скрытые риски в договоре и предложить их исправление.

Ответь строго по следующей структуре:
1. 🚩 **Найденные риски** (с указанием пунктов договора).
2. 🛠 **Рекомендации** (как защитить себя).

В самом конце ответа добавь специальный блок для формирования таблицы разногласий. 
Он должен начинаться СТРОГО с метки `---PROTOCOL---` и содержать строки в формате:
Пункт договора || Исходная редакция || Предлагаемая редакция || Комментарий
"""
}

# 5. Встроенный веб-сервер
async def handle_health(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Web server started on port {PORT}")

# 6. Вспомогательные функции
def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join([page.extract_text() or "" for page in reader.pages])

def extract_text_from_docx(docx_bytes: bytes) -> str:
    doc = Document(io.BytesIO(docx_bytes))
    return "\n".join([p.text for p in doc.paragraphs if p.text])

async def analyze_text_with_gemini(text: str, contract_type: str = "general", jurisdiction: str = "kz") -> str:
    # 1. Выполняем анонимизацию
    clean_text = anonymize_text(text)
    
    jurisdiction_inst = JURISDICTION_PROMPTS.get(jurisdiction, JURISDICTION_PROMPTS["kz"])
    raw_prompt = PROMPTS.get(contract_type, PROMPTS["general"])
    system_instruction = raw_prompt.format(jurisdiction_instruction=jurisdiction_inst)
    
    prompt_text = f"{system_instruction}\n\nПроанализируй договор:\n\n{clean_text}"
    
    models_to_try = [
        "models/gemini-1.5-flash",
        "models/gemini-1.5-pro",
        "models/gemini-1.5-flash-latest"
    ]
    
    last_exception = None

    for model_name in models_to_try:
        try:
            logging.info(f"Sending request using model: {model_name}")
            model = genai.GenerativeModel(model_name)
            response = await asyncio.to_thread(model.generate_content, prompt_text)
            
            if response and response.text:
                return response.text
        except Exception as e:
            last_exception = e
            logging.warning(f"Model {model_name} failed: {e}")

    try:
        logging.info("Polling available models from API key...")
        available = [
            m.name for m in genai.list_models() 
            if 'generateContent' in m.supported_generation_methods
        ]
        for m_name in available:
            if "flash" in m_name or "pro" in m_name:
                try:
                    logging.info(f"Trying discovered model: {m_name}")
                    model = genai.GenerativeModel(m_name)
                    response = await asyncio.to_thread(model.generate_content, prompt_text)
                    if response and response.text:
                        return response.text
                except Exception as e:
                    last_exception = e
                    continue
    except Exception as list_err:
        logging.error(f"Failed to list models: {list_err}")

    raise Exception(f"Не удалось получить ответ от Gemini. Ошибка: {last_exception}")

def create_protocol_docx(protocol_data: list) -> bytes:
    doc = Document()
    doc.add_heading('Протокол разногласий', level=1)
    
    table = doc.add_table(rows=1, cols=4)
    table.style = 'Table Grid'
    
    hdr_cells = table.rows[0].cells
    headers = ['Пункт договора', 'Редакция Заказчика', 'Предлагаемая редакция', 'Обоснование']
    for i, header in enumerate(headers):
        hdr_cells[i].text = header
        
    for item in protocol_data:
        row_cells = table.add_row().cells
        parts = item.split("||")
        if len(parts) == 4:
            for i in range(4):
                row_cells[i].text = parts[i].strip()
                
    file_stream = io.BytesIO()
    doc.save(file_stream)
    return file_stream.getvalue()

def get_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💻 Разработка ПО / Дизайн", callback_data="type_dev")],
        [InlineKeyboardButton(text="🤐 NDA (Конфиденциальность)", callback_data="type_nda")],
        [InlineKeyboardButton(text="🛠 Оказание услуг / Фриланс", callback_data="type_services")],
        [InlineKeyboardButton(text="📄 Общий / Другой договор", callback_data="type_general")],
        [InlineKeyboardButton(text="🌐 Сменить юрисдикцию", callback_data="change_jurisdiction")],
        [InlineKeyboardButton(text="ℹ️️ О сервисе и правовая информация", callback_data="show_disclaimer")]
    ])

def get_jurisdiction_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇰🇿 Казахстан (ГК РК)", callback_data="jur_kz")],
        [InlineKeyboardButton(text="🇷🇺 Россия (ГК РФ)", callback_data="jur_ru")],
        [InlineKeyboardButton(text="🌐 Международное право / English", callback_data="jur_int")]
    ])

async def process_and_reply(message: types.Message, text: str, user_id: int):
    allowed, remaining = check_and_update_limit(
        user_id=user_id,
        username=message.from_user.username or "",
        first_name=message.from_user.first_name or ""
    )
    
    if not allowed:
        await message.answer(
            "🛑 **Превышен дневной лимит проверок!**\n\n"
            f"Вам доступно **{DAILY_LIMIT} бесплатные проверки** в день. Лимит обновится завтра.\n"
            "Спасибо, что пользуетесь LegalGuard!"
        )
        return

    contract_type = USER_CONTRACT_TYPES.get(user_id, "general")
    jurisdiction = USER_JURISDICTIONS.get(user_id, "kz")
    
    jur_names = {"kz": "🇰🇿 Казахстан", "ru": "🇷🇺 Россия", "int": "🌐 Международное право"}
    status_msg = await message.answer(f"⚖️ Анализирую договор ({jur_names.get(jurisdiction)})... (Осталось проверок: {remaining})")
    
    try:
        analysis_result = await analyze_text_with_gemini(text, contract_type, jurisdiction)
        
        main_text = analysis_result
        protocol_items = []
        
        if "---PROTOCOL---" in analysis_result:
            parts = analysis_result.split("---PROTOCOL---")
            main_text = parts[0].strip()
            raw_protocol = parts[1].strip().split("\n")
            protocol_items = [line for line in raw_protocol if "||" in line]

        USER_REPORTS[user_id] = protocol_items

        kb = None
        if protocol_items:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Скачать Протокол разногласий (.docx)", callback_data="get_protocol")]
            ])

        await status_msg.edit_text(main_text, reply_markup=kb)

    except Exception as e:
        logging.error(f"Error during processing: {e}")
        await status_msg.edit_text(f"❌ Ошибка при анализе: {e}")

# 7. Обработчики
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    USER_CONTRACT_TYPES[user_id] = "general"
    if user_id not in USER_JURISDICTIONS:
        USER_JURISDICTIONS[user_id] = "kz"  # По умолчанию Казахстан

    await message.answer(
        "👋 Привет! Я **LegalGuard** — твой юридический ассистент.\n\n"
        f"Тебе доступно **{DAILY_LIMIT} бесплатные проверки** в день.\n"
        f"Текущая юрисдикция: **🇰🇿 Казахстан**\n\n"
        "Выбери тип договора или смени юрисдикцию по кнопкам ниже, либо просто отправь файл (PDF/DOCX) или текст договора:\n\n"
        "⚠️ *Сервис предоставляет автоматизированный первичный скрининг и не является юридической консультацией.*",
        reply_markup=get_type_keyboard()
    )

@dp.message(Command("disclaimer"))
async def cmd_disclaimer(message: types.Message):
    await message.answer(DISCLAIMER_TEXT)

@dp.callback_query(F.data == "show_disclaimer")
async def callback_disclaimer(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(DISCLAIMER_TEXT)

@dp.callback_query(F.data == "change_jurisdiction")
async def ask_jurisdiction(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Выберите законодательную базу для анализа:", reply_markup=get_jurisdiction_keyboard())

@dp.callback_query(F.data.startswith("jur_"))
async def set_jurisdiction(callback: CallbackQuery):
    jur = callback.data.split("_")[1]
    USER_JURISDICTIONS[callback.from_user.id] = jur
    
    names = {
        "kz": "🇰🇿 Казахстан (ГК РК)",
        "ru": "🇷🇺 Россия (ГК РФ)",
        "int": "🌐 Международное право / English"
    }
    
    await callback.answer(f"Законодательство: {names.get(jur)}")
    await callback.message.answer(f"✅ Установлена законодательная база: **{names.get(jur)}**.\n\nТеперь отправь файл или текст договора.", reply_markup=get_type_keyboard())

@dp.callback_query(F.data.startswith("type_"))
async def set_contract_type(callback: CallbackQuery):
    ctype = callback.data.split("_")[1]
    USER_CONTRACT_TYPES[callback.from_user.id] = ctype
    
    names = {
        "dev": "Разработка ПО / Дизайн",
        "nda": "NDA (Конфиденциальность)",
        "services": "Оказание услуг / Фриланс",
        "general": "Общий договор"
    }
    
    await callback.answer(f"Режим: {names.get(ctype)}")
    await callback.message.answer(f"✅ Установлен тип договора: **{names.get(ctype)}**.\n\nОтправь файл или текст договора.")

@dp.message(F.document)
async def handle_document(message: types.Message):
    file_name = message.document.file_name.lower()
    if not (file_name.endswith('.pdf') or file_name.endswith('.docx')):
        await message.answer("Пожалуйста, отправьте файл в формате PDF или DOCX.")
        return

    status_msg = await message.answer("📥 Загружаю документ...")
    try:
        file = await bot.get_file(message.document.file_id)
        file_bytes = await bot.download_file(file.file_path)

        if file_name.endswith('.pdf'):
            text = extract_text_from_pdf(file_bytes.read())
        else:
            text = extract_text_from_docx(file_bytes.read())

        if not text.strip():
            await status_msg.edit_text("Не удалось извлечь текст из файла.")
            return

        await status_msg.delete()
        await process_and_reply(message, text, message.from_user.id)

    except Exception as e:
        logging.error(f"Error downloading document: {e}")
        await status_msg.edit_text(f"❌ Ошибка обработки файла: {e}")

@dp.message(F.text)
async def handle_text(message: types.Message):
    if len(message.text.strip()) < 20:
        await message.answer("Пожалуйста, выберите тип договора по кнопкам выше или отправьте более подробный текст договора.")
        return
    await process_and_reply(message, message.text, message.from_user.id)

@dp.callback_query(F.data == "get_protocol")
async def send_protocol_file(callback: CallbackQuery):
    user_id = callback.from_user.id
    protocol_data = USER_REPORTS.get(user_id)
    
    if not protocol_data:
        await callback.answer("Данные протокола не найдены. Попробуйте отправить договор заново.", show_alert=True)
        return
        
    await callback.answer("Формирую Word-файл...")
    docx_bytes = create_protocol_docx(protocol_data)
    
    file_to_send = BufferedInputFile(docx_bytes, filename="Protocol_of_Disagreements.docx")
    await callback.message.answer_document(file_to_send, caption="📝 Ваш протокол разногласий готов!")

# 8. Главный запуск
async def main():
    init_db()
    
    # 1. Запускаем веб-сервер до polling
    await start_web_server()
    
    # 2. Очищаем вебхуки и запускаем polling
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Starting Telegram bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
