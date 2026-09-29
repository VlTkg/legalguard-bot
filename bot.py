import os
import asyncio
import json
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
import pypdf
import docx
from google import genai
from google.genai import types as genai_types

# 1. Токены из переменных окружения Render
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 2. Промпт для ИИ
SYSTEM_PROMPT = """
Ты — профессиональный юридический ассистент, специализирующийся на анализе договоров для фрилансеров и IT-специалистов.
Проанализируй предоставленный текст договора и верни ответ СТРОГО в формате JSON без кавычек markdown (```json).

Формат JSON:
{
  "score": "Оценка рисков от 1 до 10 (где 10 — очень опасно)",
  "summary": "Краткое резюме договора в 2-3 предложениях",
  "risks": ["Риск 1", "Риск 2", "Риск 3"],
  "recommendations": ["Рекомендация 1", "Рекомендация 2"]
}
"""

# 3. Веб-сервер для прохождения проверки Render (Health Check)
async def handle_health_check(request):
    return web.Response(text="LegalGuard Bot is live and healthy!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render передает порт через переменную PORT, по умолчанию 10000
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f" Web server successfully started on port {port}")

# 4. Обработчик /start
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я **LegalGuard** — твой ИИ-юрист.\n\n"
        "Отправь мне текст договора сообщением или загрузи документ в формате **PDF** или **DOCX**, "
        "и я найду подводные камни и риски!"
    )

# 5. Вызов модели Gemini с автоматическим повтором при перегрузке (Retry)
async def analyze_text_with_gemini(text: str) -> str:
    # Используем строго поддерживаемую модель gemini-3.8-flash
    model_name = "gemini-3.8-flash"
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=f"Проанализируй договор:\n\n{text}",
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.2,
                ),
            )
            return response.text
        except Exception as e:
            # Если это последняя попытка — выбрасываем ошибку
            if attempt == max_retries - 1:
                raise e
            # Если сервер перегружен (503) или произошла ошибка, ждем 2 секунды и пробуем снова
            await asyncio.sleep(2)
# 6. Форматирование ответа
def format_analysis_response(json_str: str) -> str:
    try:
        clean_str = json_str.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_str)
        
        score = data.get("score", "N/A")
        summary = data.get("summary", "Нет резюме")
        risks = "\n".join([f"⚠️ {r}" for r in data.get("risks", [])])
        recommendations = "\n".join([f"💡 {r}" for r in data.get("recommendations", [])])
        
        return (
            f"⚖️ **РЕЗУЛЬТАТ АНАЛИЗА ДОГОВОРА**\n\n"
            f"📊 **Уровень риска:** {score}/10\n\n"
            f"📝 **Краткое резюме:**\n{summary}\n\n"
            f"🚩 **Найденные риски:**\n{risks}\n\n"
            f"🛠 **Рекомендации:**\n{recommendations}"
        )
    except Exception:
        return f"📋 **Результат анализа:**\n\n{json_str}"

# 7. Обработка сообщений с текстом
@dp.message(F.text)
async def handle_text(message: types.Message):
    status_msg = await message.answer("🔍 Анализирую текст договора... Подождите 5-10 секунд.")
    try:
        raw_result = await analyze_text_with_gemini(message.text)
        formatted_result = format_analysis_response(raw_result)
        await status_msg.edit_text(formatted_result, parse_mode="Markdown")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при анализе: {str(e)}")

# 8. Обработка файлов PDF и DOCX
@dp.message(F.document)
async def handle_document(message: types.Message):
    doc_name = message.document.file_name.lower()
    
    if not (doc_name.endswith('.pdf') or doc_name.endswith('.docx')):
        await message.answer("⚠️ Пожалуйста, отправьте файл формата .pdf или .docx")
        return

    status_msg = await message.answer("📥 Загружаю и читаю файл...")
    file_path = f"downloads/{message.document.file_name}"
    os.makedirs("downloads", exist_ok=True)

    try:
        file_info = await bot.get_file(message.document.file_id)
        await bot.download_file(file_info.file_path, file_path)
        
        extracted_text = ""
        if doc_name.endswith('.pdf'):
            reader = pypdf.PdfReader(file_path)
            for page in reader.pages:
                extracted_text += page.extract_text() + "\n"
        elif doc_name.endswith('.docx'):
            doc_file = docx.Document(file_path)
            for p in doc_file.paragraphs:
                extracted_text += p.text + "\n"

        if not extracted_text.strip():
            await status_msg.edit_text("❌ Не удалось извлечь текст из файла. Убедитесь, что это не сканированное изображение.")
            return

        await status_msg.edit_text("🔍 Текст извлечен! Передаю юристу Gemini...")
        raw_result = await analyze_text_with_gemini(extracted_text)
        formatted_result = format_analysis_response(raw_result)
        await status_msg.edit_text(formatted_result, parse_mode="Markdown")

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка обработки файла: {str(e)}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# 9. Точка входа: одновременно запускаем веб-сервер и бота Telegram
async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
