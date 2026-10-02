import os
import json
import threading
from flask import Flask, request, jsonify, send_from_directory
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
WEBAPP_URL = os.environ.get('WEBAPP_URL', 'https://legalguard-bot.onrender.com')

# Вставьте ваш числовой Telegram ID (чтобы получать сообщения о багах)
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', '123456789'))

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder='.')

# Временное хранилище отчетов пользователей
user_reports = {}

# --- TELEGRAM BOT HANDLERS ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    welcome_text = (
        " 👋 **Приветствую! Я LegalGuard.**\n\n"
        "Я умею проводить экспресс-анализ договоров и подсвечивать ключевые риски.\n"
        "Отправьте мне документ (PDF, DOCX) или текст договора для проверки."
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

@bot.message_handler(content_types=['document', 'text'])
def handle_document(message):
    user_id = message.from_user.id
    
    msg = bot.reply_to(message, "⏳ **Анализирую документ...** Пожалуйста, подождите.", parse_mode="Markdown")
    
    # Имитация юридического анализа для примера (здесь подключается ваша модель/Gemini)
    mock_report = {
        "doc_title": "Договор возмездного оказания услуг",
        "summary": { "high": 1, "medium": 1, "low": 1 },
        "risks": [
            {
                "id": 1,
                "clause": "4.2 (Оплата)",
                "level": "high",
                "comment": "Отсутствует четкий срок оплаты после подписания акта.",
                "original": "Оплата производится Заказчиком по мере поступления средств.",
                "proposed": "Оплата производится Заказчиком в течение 5 (пяти) банковских дней с момента подписания Акта."
            },
            {
                "id": 2,
                "clause": "7.1 (Ответственность)",
                "level": "med",
                "comment": "Завышенная неустойка за просрочку.",
                "original": "За каждый день просрочки Исполнитель уплачивает пеню в размере 1% от суммы.",
                "proposed": "За каждый день просрочки Исполнитель уплачивает пеню в размере 0.1% от суммы."
            },
            {
                "id": 3,
                "clause": "10.3 (Подсудность)",
                "level": "low",
                "comment": "Договорная подсудность по месту нахождения Заказчика.",
                "original": "Все споры рассматриваются в Арбитражном суде г. Москвы.",
                "proposed": "Споры рассматриваются в соответствии с действующим законодательством РФ."
            }
        ]
    }
    
    # Сохраняем отчет в память по user_id
    user_reports[user_id] = mock_report

    # Формируем кнопку для открытия WebApp
    markup = InlineKeyboardMarkup()
    web_app_full_url = f"{WEBAPP_URL}?user_id={user_id}"
    web_app_btn = InlineKeyboardButton("📊 Открыть полный интерактивный отчет", web_app=WebAppInfo(url=web_app_full_url))
    markup.add(web_app_btn)

    bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=msg.message_id,
        text="✅ **Анализ завершен!**\n\nНажмите на кнопку ниже, чтобы открыть интерактивный разбор рисков:",
        reply_markup=markup,
        parse_mode="Markdown"
    )

# --- FLASK WEBAPP ENDPOINTS ---
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/report', methods=['GET'])
def get_report():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "Missing user_id"}), 400
    
    report = user_reports.get(int(user_id))
    if not report:
        return jsonify({"error": "Report not found"}), 404
        
    return jsonify(report)

@app.route('/api/feedback', methods=['POST'])
def handle_feedback():
    data = request.get_json() or {}
    user_id = data.get('user_id')
    action = data.get('action')
    
    if action == 'clause_vote':
        clause_id = data.get('clause_id')
        vote = data.get('vote')
        print(f"[FEEDBACK] Пользователь {user_id} поставил {vote} пункту #{clause_id}")
        
    elif action == 'rating':
        stars = data.get('stars')
        print(f"[RATING] Пользователь {user_id} оценил работу на {stars} звёзд")

    return jsonify({"status": "ok"})

@app.route('/api/bugreport', methods=['POST'])
def handle_bugreport():
    data = request.get_json() or {}
    user_id = data.get('user_id')
    user_name = data.get('user_name', 'Без имени')
    report_text = data.get('report', '')

    print(f"[BUG REPORT] User {user_id} (@{user_name}): {report_text}")

    # Отправка багрепорта администратору
    msg_to_admin = (
        f"🚨 <b>Багрепорт / Неполадка в WebApp!</b>\n\n"
        f"<b>От кого:</b> @{user_name} (ID: <code>{user_id}</code>)\n"
        f"<b>Описание проблемы:</b>\n{report_text}"
    )

    try:
        bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg_to_admin, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка отправки сообщения админу: {e}")

    return jsonify({"status": "ok"})

# --- ЗАПУСК БОТА И СЕРВЕРА ---
def run_bot():
    bot.remove_webhook()
    bot.infinity_polling(skip_pending=True)

if __name__ == '__main__':
    # Запуск бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()

    # Запуск веб-сервера Render
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
