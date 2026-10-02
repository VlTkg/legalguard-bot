import os
import io
import json
import logging
import google.generativeai as genai
from quart import Quart, request, jsonify, send_from_directory
from pypdf import PdfReader
import docx
from odf import text
from odf.opendocument import load

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LegalGuard-Backend")

app = Quart(__name__, static_folder=".")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

def extract_text_from_file(file_bytes, filename):
    """Извлечение текста из PDF, DOCX, ODT, TXT, RTF с подробной обработкой ошибок"""
    filename_lower = filename.lower()
    
    try:
        # PDF
        if filename_lower.endswith('.pdf'):
            reader = PdfReader(io.BytesIO(file_bytes))
            pages_text = [page.extract_text() for page in reader.pages if page.extract_text()]
            return "\n".join(pages_text)
            
        # DOCX / DOC
        elif filename_lower.endswith(('.docx', '.doc')):
            doc = docx.Document(io.BytesIO(file_bytes))
            paragraphs = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
            return "\n".join(paragraphs)

        # ODT (OpenDocument Text)
        elif filename_lower.endswith('.odt'):
            odt_doc = load(io.BytesIO(file_bytes))
            extracted = []
            for p in odt_doc.getElementsByType(text.P):
                p_text = "".join([node.data for node in p.childNodes if node.nodeType == 3])
                if p_text.strip():
                    extracted.append(p_text)
            return "\n".join(extracted)

        # TXT / RTF (Кодировки UTF-8, CP1251, Latin1)
        elif filename_lower.endswith(('.txt', '.rtf')):
            for enc in ['utf-8', 'cp1251', 'windows-1251', 'latin1']:
                try:
                    return file_bytes.decode(enc)
                except (UnicodeDecodeError, TypeError):
                    continue
            return file_bytes.decode('utf-8', errors='ignore')
            
        else:
            # Резервный вариант для прочих текстовых файлов
            for enc in ['utf-8', 'cp1251']:
                try:
                    return file_bytes.decode(enc)
                except Exception:
                    continue
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

        # Обработка файла
        if 'file' in files:
            uploaded_file = files['file']
            file_bytes = uploaded_file.read()
            # В Quart read() возвращает байты или корутину
            if hasattr(file_bytes, '__await__'):
                file_bytes = await file_bytes

            if file_bytes:
                extracted = extract_text_from_file(file_bytes, uploaded_file.filename)
                if extracted and extracted.strip():
                    document_text = extracted

        if not document_text or not document_text.strip():
            return jsonify({
                "doc_title": "Ошибка извлечения текста",
                "summary": {"high": 1, "medium": 0, "low": 0},
                "risks": [{
                    "clause": "Файл / Текст",
                    "level": "high",
                    "comment": "Не удалось прочитать текст из загруженного файла. Проверьте, что файл не пустой и не защищен от чтения.",
                    "original": "",
                    "proposed": ""
                }]
            }), 200

        prompt = f"""
Ты опытный корпоративный юрист. Проведи экспресс-анализ договора.
Юрисдикция: {jurisdiction}
Категория: {category}

Текст договора:
---
{document_text[:15000]}
---

Ответь СТРОГО в формате JSON без какого-либо дополнительного текста или маркдаун-разметки:
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
Значения для "level": только "high", "med", "low".
"""

        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content(prompt)

        raw_response = response.text.strip()
        if raw_response.startswith('```json'):
            raw_response = raw_response[7:]
        if raw_response.startswith('```'):
            raw_response = raw_response[3:]
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
                "clause": "Сбой сервера",
                "level": "high",
                "comment": f"Произошла ошибка при обработке: {str(e)}",
                "original": "",
                "proposed": ""
            }]
        }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
