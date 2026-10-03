import os
import io
import json
import logging
import asyncio
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as AsyncTimeoutError
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import asyncpg
import google.generativeai as genai
from quart import Quart, request, jsonify, send_from_directory, render_template_string
from pypdf import PdfReader
import docx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LegalGuard-Backend")

app = Quart(__name__, static_folder=".")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

executor = ThreadPoolExecutor(max_workers=4)
db_pool = None


async def init_db():
    """Инициализация пула соединений PostgreSQL и создание таблицы"""
    global db_pool
    if not DATABASE_URL:
        logger.error("DATABASE_URL не задана в переменных окружения Render!")
        return

    # Render иногда выдает URI с 'postgres://', asyncpg требует 'postgresql://'
    postgres_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    try:
        db_pool = await asyncpg.create_pool(postgres_url)
        async with db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS feedbacks (
                    id SERIAL PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    category TEXT,
                    rating INTEGER,
                    thumb TEXT,
                    comment TEXT
                )
            ''')
        logger.info("База данных PostgreSQL успешно инициализирована.")
    except Exception as e:
        logger.error(f"Ошибка подключения к PostgreSQL: {e}")


@app.before_serving
async def startup():
    await init_db()


def parse_odt_file(file_bytes):
    """Извлечение текста из ODT (ZipFile + ElementTree)"""
    try:
        with ZipFile(io.BytesIO(file_bytes)) as z:
            xml_content = z.read('content.xml')
            tree = ET.fromstring(xml_content)
            
            paragraphs = []
            for elem in tree.iter():
                if elem.tag.endswith('p') or elem.tag.endswith('h'):
                    text = "".join(elem.itertext()).strip()
                    if text:
                        paragraphs.append(text)
            return "\n".join(paragraphs)
    except Exception as e:
        logger.error(f"Ошибка парсинга ODT: {e}")
        return ""


def extract_text_from_file(file_bytes, filename):
    """Универсальное извлечение текста из файлов"""
    if not file_bytes:
        return ""

    filename_lower = filename.lower()
    logger.info(f"Обработка файла {filename} ({len(file_bytes)} байт)")

    try:
        # 1. TXT / RTF
        if filename_lower.endswith('.txt'):
            for enc in ['utf-8-sig', 'utf-8', 'cp1251', 'windows-1251', 'utf-16', 'latin1']:
                try:
                    text = file_bytes.decode(enc).strip()
                    if text:
                        return text
                except (UnicodeDecodeError, TypeError):
                    continue
            return file_bytes.decode('utf-8', errors='ignore')

        # 2. ODT
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

        else:
            return file_bytes.decode('utf-8', errors='ignore')

    except Exception as e:
        logger.error(f"Ошибка извлечения текста из {filename}: {e}")
        return ""


def get_available_flash_models():
    """Динамическое получение списка доступных flash-моделей"""
    try:
        available_models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                if 'flash' in m.name:
                    model_id = m.name.replace('models/', '')
                    available_models.append(model_id)
        
        logger.info(f"Доступные flash-модели: {available_models}")
        return available_models
    except Exception as e:
        logger.error(f"Не удалось получить список моделей: {e}")
        return []


def call_gemini_sync(prompt, model_name):
    """Синхронный вызов модели"""
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(prompt)
    return response.text if response else None


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

            if file_bytes:
                extracted = extract_text_from_file(file_bytes, uploaded_file.filename)
                if extracted and extracted.strip():
                    document_text = extracted

        if not document_text or not document_text.strip():
            return jsonify({
                "doc_title": "Ошибка чтения файла",
                "summary": {"high": 1, "medium": 0, "low": 0},
                "risks": [{
                    "clause": "Проверка файла",
                    "level": "high",
                    "comment": "Не удалось прочитать текст из файла. Проверьте, что файл не пустой.",
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
{document_text[:10000]}
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

        raw_text = None
        loop = asyncio.get_event_loop()

        candidate_models = await loop.run_in_executor(executor, get_available_flash_models)
        
        if not candidate_models:
            candidate_models = ['gemini-2.0-flash', 'gemini-1.5-flash']

        for model_name in candidate_models:
            try:
                logger.info(f"Пробуем модель: {model_name}...")
                future = loop.run_in_executor(executor, call_gemini_sync, prompt, model_name)
                raw_text = await asyncio.wait_for(future, timeout=15.0)
                if raw_text:
                    logger.info(f"Успешный ответ от {model_name}")
                    break
            except (AsyncTimeoutError, Exception) as err:
                logger.warning(f"Ошибка или таймаут модели {model_name}: {err}")
                continue

        if not raw_text:
            raise Exception("Ни одна из доступных моделей Gemini не вернула ответ. Проверьте логи на Render.")

        raw_text = raw_text.strip()
        if raw_text.startswith('```json'):
            raw_text = raw_text[7:]
        if raw_text.startswith('```'):
            raw_text = raw_text[3:]
        if raw_text.endswith('```'):
            raw_text = raw_text[:-3]

        result_json = json.loads(raw_text.strip())
        return jsonify(result_json)

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}")
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


@app.route('/api/feedback', methods=['POST'])
async def save_feedback():
    """Сохранение обратной связи в PostgreSQL + вывод в логи"""
    try:
        data = await request.get_json()
        category = data.get('category', 'quality')
        rating = data.get('rating', 0)
        thumb = data.get('thumb', '')
        comment = data.get('comment', '')
        created_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        # 1. Запись в PostgreSQL ($1, $2... вместо ? в SQLite)
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO feedbacks (created_at, category, rating, thumb, comment)
                    VALUES ($1, $2, $3, $4, $5)
                ''', created_at, category, rating, thumb, comment)

        # 2. Форматированный вывод в Логи Render
        stars_str = '★' * rating + '☆' * (5 - rating) if rating > 0 else 'Без оценки'
        thumb_str = '👍 Полезно' if thumb == 'up' else ('👎 Замечание' if thumb == 'down' else 'Не указано')
        cat_str = '🔴 Технический баг' if category == 'tech_issue' else '🟢 Качество анализа'

        logger.info(
            f"\n==================== [ НОВЫЙ ОТЗЫВ ] ====================\n"
            f" Время:       {created_at}\n"
            f" Категория:   {cat_str}\n"
            f" Оценка:      {stars_str} ({rating}/5)\n"
            f" Лайк/Диз:    {thumb_str}\n"
            f" Комментарий: {comment if comment else '(без комментария)'}\n"
            f"=========================================================="
        )

        return jsonify({"status": "success", "message": "Feedback saved"}), 200

    except Exception as e:
        logger.error(f"Ошибка сохранения отзыва: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/admin/feedback')
async def view_feedback_admin():
    """Панель просмотра отзывов из PostgreSQL"""
    rows = []
    if db_pool:
        async with db_pool.acquire() as conn:
            records = await conn.fetch("SELECT * FROM feedbacks ORDER BY id DESC")
            # Преобразуем asyncpg Record в словари для удобства Jinja2
            rows = [dict(record) for record in records]

    html = '''
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>LegalGuard — Панель отзывов</title>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 20px; background: #f4f6f9; }
            h1 { color: #1a202c; font-size: 20px; margin-bottom: 20px; }
            table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
            th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #edf2f7; font-size: 14px; }
            th { background: #2d3748; color: #fff; font-weight: 600; }
            tr:hover { background: #f8fafc; }
            .stars { color: #f6ad55; font-weight: bold; }
            .badge-tech { background: #fed7d7; color: #9b2c2c; padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }
            .badge-quality { background: #c6f6d5; color: #22543d; padding: 3px 8px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>Реестр обратной связи пользователей</h1>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Дата (UTC)</th>
                    <th>Тип</th>
                    <th>Оценка</th>
                    <th>Лайк/Дизлайк</th>
                    <th>Текст комментария</th>
                </tr>
            </thead>
            <tbody>
                {% for row in rows %}
                <tr>
                    <td>{{ row['id'] }}</td>
                    <td>{{ row['created_at'] }}</td>
                    <td>
                        {% if row['category'] == 'tech_issue' %}
                            <span class="badge-tech">Баг / Тех вопр</span>
                        {% else %}
                            <span class="badge-quality">Качество</span>
                        {% endif %}
                    </td>
                    <td class="stars">★ {{ row['rating'] }}/5</td>
                    <td>{{ '👍' if row['thumb'] == 'up' else ('👎' if row['thumb'] == 'down' else '—') }}</td>
                    <td>{{ row['comment'] if row['comment'] else '<i>(без текста)</i>' }}</td>
                </tr>
                {% else %}
                <tr><td colspan="6" style="text-align:center; color: #a0aec0;">Отзывов пока нет</td></tr>
                {% endfor %}
            </tbody>
        </table>
    </body>
    </html>
    '''
    return await render_template_string(html, rows=rows)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
