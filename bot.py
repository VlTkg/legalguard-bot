import os
import json
import threading
from flask import Flask, request, jsonify, send_from_directory
import telebot
from telebot.types import MenuButtonWebApp, WebAppInfo

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get('BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
WEBAPP_URL = os.environ.get('WEBAPP_URL', 'https://legalguard-bot.onrender.com')
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', '123456789'))

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder='.')

# При старте бота устанавливаем кнопку меню слева от строки ввода
def setup_menu_button():
    try:
        bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="🛡 Открыть LegalGuard",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
    except Exception as e:
        print(f"Ошибка установки MenuButton: {e}")

# --- TELEGRAM BOT HANDLERS ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    welcome_text = (
        " 👋 **Приветствую! Я LegalGuard.**\n\n"
        "Нажмите на кнопку **«🛡 Открыть LegalGuard»** внизу слева, чтобы запустить приложение и сразу вставить текст договора на проверку!"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

# --- FLASK WEBAPP ENDPOINTS ---
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/analyze', methods=['POST'])
def analyze_text():
    data = request.get_json() or {}
    user_id = data.get('user_id')
    text = data.get('text', '')

    print(f"[ANALYZE] Запрос от {user_id}, длина текста: {len(text)} символов")

    # Здесь выполняется логика анализа через Gemini / вашу модель
    mock_report = {
        "doc_title": "Экспресс-анализ договора",
        "summary": { "high": 1, "medium": 1, "low": 1 },
        "risks": [
            {
                "id": 1,
                "clause": "4.2 (Оплата)",
                "level": "high",
                "comment": "Отсутствует четкий срок оплаты после подписания акта.",
                "original": text[:100] + "..." if len(text) > 100 else text,
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

    return jsonify(mock_report)

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
    setup_menu_button()
    bot.infinity_polling(skip_pending=True)

if __name__ == '__main__':
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()

    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
