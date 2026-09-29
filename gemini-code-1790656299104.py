import os
import json
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from google import genai
from google.genai import types as genai_types
import pypdf
import docx

# Берём ключи из переменных окружения облака
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """
Ты — профессиональный юридический аудитор и защитник прав фрилансеров. 
Твоя задача — тщательно проанализировать переданный договор подряда/оказания услуг и найти все опасные, кабальные или невыгодные для исполнителя условия.

КРИТИЧЕСКОЕ ТРЕБОВАНИЕ К ФОРМАТУ:
Ты ДОЛЖЕН отвечать СТРОГО в формате валидного JSON. 
Не добавляй ВСТУПИТЕЛЬНЫХ ФРАЗ и никаких комментариев вне JSON-объекта.

СТРУКТУРА JSON:
{
  "overall_risk_level": "RED" | "YELLOW" | "GREEN",
  "summary": "Краткое резюме договора простым языком (2-3 предложения)",
  "risks": [
    {
      "severity": "RED" | "YELLOW",
      "category": "Категория",
      "clause_quote": "Точная цитата из договора",
      "explanation": "Объяснение понятным языком, в чем подвох и риск",
      "suggested_fix": "Готовый текст правки для отправки Заказчику"
    }
  ],
  "positive_aspects": ["Хорошие или безопасные пункты договора"]
}
"""

async def analyze_contract_text(text: str) -> dict:
    response = ai_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=f"Проанализируй текст договора:\n\n{text}",
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    return json.loads(response.text)

def format_analysis_response(data: dict) -> str:
    risk_emoji = {"RED": "🔴 КРИТИЧЕСКИЙ РИСК", "YELLOW": "🟡 СРЕДНИЙ РИСК", "GREEN": "🟢 БЕЗОПАСНО"}
    level = data.get("overall_risk_level", "YELLOW")
    
    msg = f"<b>{risk_emoji.get(level, level)}</b>\n\n"
    msg += f"📋 <b>Резюме:</b>\n{data.get('summary', '')}\n\n"
    
    risks = data.get("risks", [])
    if risks:
        msg += "⚠️ <b>НАЙДЕННЫЕ УЛОВКИ И РИСКИ:</b>\n\n"
        for i, r in enumerate(risks, 1):
            sev_icon = "🔴" if r.get("severity") == "RED" else "🟡"
            msg += f"{i}. {sev_icon} <b>[{r.get('category', 'Риск')}]</b>\n"
            msg += f"📜 <i>Цитата:</i> «{r.get('clause_quote')}»\n"
            msg += f"💡 <i>В чем подвох:</i> {r.get('explanation')}\n"
            msg += f"✏️ <i>Как исправить:</i> <code>{r.get('suggested_fix')}</code>\n\n"
    else:
        msg += "✅ Критических рисков в тексте не обнаружено.\n"
        
    return msg

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я <b>LegalGuard</b> — твой ИИ-юрист.\n\n"
        "Отправь мне текст договора (Ctrl+C / Ctrl+V) или загрузи файл в формате <b>PDF</b> или <b>DOCX</b>.\n"
        "За 15 секунд я подсвечу все скрытые штрафы и опасные условия!"
    )

@dp.message(F.text)
async def handle_text(message: types.Message):
    status_msg = await message.answer("🔍 Анализирую текст договора...")
    try:
        result = await analyze_contract_text(message.text)
        formatted_text = format_analysis_response(result)
        await status_msg.edit_text(formatted_text, parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при анализе текста: {str(e)}")

@dp.message(F.document)
async def handle_document(message: types.Message):
    document = message.document
    file_name = document.file_name.lower()
    
    if not (file_name.endswith('.pdf') or file_name.endswith('.docx')):
        await message.answer("⚠️ Пожалуйста, отправьте файл в формате PDF или DOCX.")
        return

    status_msg = await message.answer("📥 Скачиваю и читаю файл...")
    file_path = f"temp_{document.file_id}_{file_name}"
    
    try:
        await bot.download(document, destination=file_path)
        extracted_text = ""
        
        if file_name.endswith('.pdf'):
            reader = pypdf.PdfReader(file_path)
            for page in reader.pages:
                extracted_text += page.extract_text() or ""
        elif file_name.endswith('.docx'):
            doc = docx.Document(file_path)
            extracted_text = "\n".join([p.text for p in doc.paragraphs])
            
        await status_msg.edit_text("⚡ Текст извлечен. Запускаю ИИ-аудит Gemini...")
        result = await analyze_contract_text(extracted_text)
        formatted_text = format_analysis_response(result)
        await status_msg.edit_text(formatted_text, parse_mode="HTML")
        
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка обработки файла: {str(e)}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())