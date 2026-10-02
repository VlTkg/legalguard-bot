import os
import io
import json
import logging
from zipfile import ZipFile
import xml.etree.ElementTree as ET

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


def parse_odt_file(file_bytes):
    """Надежное извлечение текста из ODT файла через встроенный zipfile и ElementTree"""
    try:
        with ZipFile(io.BytesIO(file_bytes)) as z:
            xml_content = z.read('content.xml')
            tree = ET.fromstring(xml_content)
            
            paragraphs = []
            # Проходим по всем элементам XML и собираем текст из тегов параграфов
            for elem in tree.iter():
                if elem.tag.endswith('p') or elem.tag.endswith('h'):
                    text = "".join(elem.itertext()).strip()
                    if text:
                        paragraphs.append(text)
            return "\n".join(paragraphs)
    except Exception as e:
        logger.error(f"Ошибка парсинга ODT через ZipFile: {e}")
        return ""


def extract_text_from_file(file_bytes, filename):
    """Универсальный извлекатель текста для PDF, DOCX, ODT, TXT"""
    if not file_bytes:
        return ""

    filename_lower = filename.lower()
    logger.info(f"Начало извлечения текста из файла: {filename} (размер: {len(file_bytes)} байт)")

    try:
        # 1. TXT / RTF / Простой текст
        if filename_lower.endswith('.txt'):
            for enc in ['utf-8', 'cp1251', 'windows-1251', 'utf-16', 'latin1']:
                try:
                    text = file_bytes.decode(enc).strip()
                    if text:
                        return text
                except (UnicodeDecodeError, TypeError):
                    continue
            return file_bytes.decode('utf-8', errors='ignore')

        # 2. ODT (OpenDocument Text)
        elif filename_lower.endswith('.odt'):
            return parse_odt_file(file_bytes)

        # 3. PDF
        elif filename_lower.endswith('.pdf'):
            reader = PdfReader(io.BytesIO(file_bytes))
            pages_text = [page.extract_text() for page in reader.pages if page.extract_text()]
            return "\n".join(pages_text)

        # 4. DOCX / DOC
        elif filename_lower.endswith(('.docx', '.doc')):
            doc = docx.Document(io.BytesIO(file_bytes))
            paragraphs = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
            return "\n".join(paragraphs)

        # Если формат не определен по расширению, пробуем как декодировать текст
        else:
            return file_bytes.decode('utf-8', errors='ignore')

    except Exception as e:
        logger.error(f"Критическая ошибка при обработке {filename}: {e}")
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

        # Важно для Quart: чтение файла через AWAIT
        if 'file' in files:
            uploaded_file = files['file']
            file_bytes = await uploaded_file.read()

            if file_bytes:
                extracted = extract_text_from_file(file_bytes, uploaded_file.filename)
                if extracted and extracted.strip():
                    document_text = extracted

        # Если текст так и не удалось получить
        if not document_text or not document_text.strip():
            logger.warning("Текст документа не извлечен или пуст.")
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

        logger.info(f"Текст успешно извлечен (длина: {len(document_text)} символов). Отправка в Gemini...")

        prompt = f"""
Ты опытный корпоративный юрист. Проведи экспресс-анализ договора.
Юрисдикция: {jurisdiction}
Категория: {category}

Текст договора:
---
{document_text[:15000]}
---

Ответь СТРОГО в формате JSON без маркдаун-разметки:
{{
  "doc_title": "Наименование договора",
  "summary": {{
    "high": 1,
    "medium": 2,
    "low": 0
  }},
  "risks": [
    {{
      "clause": "Пункт договора",
      "level": "high", 
      "comment": "Описание риска",
      "original": "Исходный текст",
      "proposed": "Безопасная формулировка"
    }}
  ]
}}
Значения для "level": строго "high", "med", "low".
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
        logger.error(f"Ошибка сервера во время анализа: {e}")
        return jsonify({
            "doc_title": "Ошибка анализа",
            "summary": {"high": 1, "medium": 0, "low": 0},
            "risks": [{
                "clause": "Сбой сервера",
                "level": "high",
                "comment": f"Произошла ошибка обработки: {str(e)}",
                "original": "",
                "proposed": ""
            }]
        }), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
