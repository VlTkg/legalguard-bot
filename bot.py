import asyncio
import io
import os
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web
from docx import Document
from google import genai
from google.genai import types as genai_types
import pypdf

# Логирование
logging.basicConfig(level=logging.INFO)

# 1. Настройка конфигурации
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 10000))

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise ValueError("Переменные окружения TELEGRAM_TOKEN или GEMINI_API_KEY не заданы!")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

USER_REPORTS = {}
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
Ты — профессиональный юридический ассистент для фрилансеров и исполнителей.
Твоя задача — найти скрытые риски в договоре и предложить их исправление.

Ответь строго по следующей структуре:
1. 🚩 **Найденные риски** (с указанием пунктов договора и объяснением человеческим языком).
2. 🛠 **Рекомендации** (как защитить себя).

В самом конце ответа добавь специальный блок для формирования таблицы разногласий. 
Он должен начинаться СТРОГО с метки `---PROTOCOL---` и содержать строки в формате:
Пункт договора || Исходная редакция || Предлагаемая редакция || Комментарий

Пример блока:
---PROTOCOL---
п. 7.2 || Штраф 100% за задержку на 1 час || Пеня 0.1% за каждый день просрочки, но не более 10% || Защита от неоплаты за минимальное отклонение от графика.
п. 7.5 || Оплата в течение 180 дней || Оплата в течение 5 рабочих дней || Предотвращение кассового разрыва.
"""

# 2. Встроенный веб-сервер
async def handle_health(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# 3. Вспомогательные функции
def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join([page.extract_text() or "" for page in reader.pages])

def extract_text_from_docx(docx_bytes: bytes) -> str:
    doc = Document(io.BytesIO(docx_bytes))
    return "\n".join([p.text for p in doc.paragraphs if p.text])

async def analyze_text_with_gemini(text: str) -> str:
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = gemini_client.models.generate_content(
                model="gemini-3.8-flash",
                contents=f"Проанализируй договор:\n\n{text}",
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.2,
                ),
            )
            return response.text
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(5)
                continue
            raise e

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

async def process_and_reply(message: types.Message, text: str):
    status_msg = await message.answer("⚖️ Анализирую договор с помощью Gemini...")
    try:
        analysis_result = await analyze_text_with_gemini(text)
        
        main_text = analysis_result
        protocol_items = []
        
        if "---PROTOCOL---" in analysis_result:
            parts = analysis_result.split("---PROTOCOL---")
            main_text = parts[0].strip()
            raw_protocol = parts[1].strip().split("\n")
            protocol_items = [line for line in raw_protocol if "||" in line]

        USER_REPORTS[message.from_user.id] = protocol_items

        kb = None
        if protocol_items:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Скачать Протокол разногласий (.docx)", callback_data="get_protocol")]
            ])

        # Без parse_mode, чтобы предотвратить ошибки форматирования Telegram
        await status_msg.edit_text(main_text, reply_markup=kb)

    except Exception as e:
        logging.error(f"Error during processing: {e}")
        await status_msg.edit_text(f"❌ Ошибка при анализе: {e}")

# 4. Обработчики
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    logging.info(f"Received /start from {message.from_user.id}")
    await message.answer(
        "👋 Привет! Я LegalGuard — твой юридический ассистент.\n\n"
        "Отправь мне файл договора (PDF или DOCX) или просто вставь текст договора сообщением.\n\n"
        "⚠️ Обратите внимание: сервис предоставляет автоматизированный первичный анализ и не является юридической консультацией."
    )

@dp.message(F.document)
async def handle_document(message: types.Message):
    logging.info(f"Received document: {message.document.file_name}")
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
        await process_and_reply(message, text)

    except Exception as e:
        logging.error(f"Error downloading document: {e}")
        await status_msg.edit_text(f"❌ Ошибка обработки файла: {e}")

@dp.message(F.text)
async def handle_text(message: types.Message):
    logging.info(f"Received text message from {message.from_user.id}")
    if len(message.text.strip()) < 20:
        await message.answer("Пожалуйста, отправьте более подробный текст договора для анализа.")
        return
    await process_and_reply(message, message.text)

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

# 5. Главный запуск
async def main():
    await start_web_server()
    # Удаляем старые вебхуки для очистки каналов получения сообщений
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
