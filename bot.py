import os
import io
import json
import logging
import google.generativeai as genai
from quart import Quart, request, jsonify, send_from_directory
from pypdf import PdfReader
import docx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LegalGuard-Backend")

app = Quart(__name__, static_folder=".")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

def extract_text_from_file(file_bytes, filename):
    """Извлечение текста из поддерживаемых форматов"""
    filename_lower = filename.lower()
    
    try:
        if filename_lower.endswith('.pdf'):
            reader = PdfReader(io.BytesIO(file_bytes))
            text = "\n".join([page.extract_text() or "" for page in reader.pages])
            return text
            
        elif filename_lower.endswith(('.docx', '.doc')):
            doc = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join([p.text for p in doc.paragraphs])
            return text
            
        elif filename_lower.endswith(('.txt', '.rtf')):
            return file_bytes.decode('utf-8', errors='ignore')
            
        else:
            # Для нетекстовых/изображений
            return file_bytes.decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"Ошибка при считывании файла {filename}: {e}")
        return ""

@app.route('/')
async def serve_webapp():
    return await send_from_directory('.', 'index.html')

@app.route('/api/analyze', methods=['POST'])
async def analyze_document():
    try:
        form = await request.form
        files = await request.files

        jurisdiction = form.get('jurisdiction', 'RU')
        category = form.get('category', 'general')
        document_text = form.get('text', '')

        if 'file' in files:
            uploaded_file = files['file']
            file_bytes = uploaded_file.read()
            extracted = extract_text_from_file(file_bytes, uploaded_file.filename)
            if extracted:
                document_text = extracted

        if not document_text.strip():
            return jsonify({"error": "Не удалось извлечь текст из документа"}), 400

        prompt = f"""
Ты опытный корпоративный юрист. Проведи экспресс-анализ договора.
Юрисдикция: {jurisdiction}
Категория: {category}

Текст договора:
---
{document_text[:15000]}
---

Ответь СТРОГО в формате JSON без какого-либо дополнительного текста или разметки ```json:
{{
  "doc_title": "Краткое наименование договора",
  "summary": {{
    "high": 1,
    "medium": 2,
    "low": 0
  }},
  "risks": [
    {{
      "clause": "Номер или название пункта",
      "level": "high", 
      "comment": "В чем заключается риск для стороны",
      "original": "Исходный текст из договора",
      "proposed": "Безопасная формулировка"
    }}
  ]
}}
Обрати внимание: "level" может принимать только значения "high", "med", "low".
"""

        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content(prompt)

        raw_response = response.text.strip()
        if raw_response.startswith('```json'):
            raw_response = raw_response[7:]
        if raw_response.endswith('```'):
            raw_response = raw_response[:-3]
            
        result_json = json.loads(raw_response.strip())
        return jsonify(result_json)

    except Exception as e:
        logger.error(f"Ошибка при обработке запроса: {e}")
        return jsonify({
            "doc_title": "Ошибка анализа",
            "summary": {"high": 1, "medium": 0, "low": 0},
            "risks": [{
                "clause": "Ошибка",
                "level": "high",
                "comment": "Не удалось проанализировать файл. Проверьте формат текста.",
                "original": "",
                "proposed": ""
            }]
        }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
