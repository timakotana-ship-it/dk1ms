import os
import json
import asyncio
import random
import string
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ========== КОНФИГУРАЦИЯ ==========
TOKEN = "8612455110:AAGhl4KqN-7zEenIIHXL4_WLh_Fp62ML8h0"  # Замени на свой токен
ADMIN_ID = 1916338307  # ID админа (твой)

# ID группы для команды /curator (None = работает в любом чате)
GROUP_ID = None  # Пример: -1001234567890

# НАСТРОЙКИ ПРОКСИ (выбери один вариант и раскомментируй)
# =================================

# Вариант 1: SOCKS5 прокси (например от 911.re, Proxy6 и т.д.)
# PROXY_URL = "socks5://логин:пароль@ip:порт"
# PROXY_URL = "socks5://127.0.0.1:9050"  # для Tor

# Вариант 2: HTTP/HTTPS прокси
# PROXY_URL = "http://логин:пароль@ip:порт"

# Вариант 3: Без прокси (если интернет прямой)
PROXY_URL = None

# =================================

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')
os.makedirs(DATA_DIR, exist_ok=True)

# Загрузка/сохранение JSON
def load_json(filename):
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.exists(filepath):
        return [] if 'users' in filename else {}
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read().strip()
        if not content:
            return [] if 'users' in filename else {}
        return json.loads(content)

def save_json(filename, data):
    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ---------- вспомогательные функции ----------

def get_curators():
    filepath = os.path.join(DATA_DIR, 'curators.json')
    default = [{'username': '@BlancSupport', 'percent': '0%'}]
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.loads(f.read().strip() or '[]')
        return data if isinstance(data, list) and data else default
    except Exception:
        return default


# Команда /curator
async def curator_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Проверяем группу, если GROUP_ID задан
    if GROUP_ID and update.effective_chat.id != GROUP_ID:
        return
    curators = get_curators()
    lines = ["👥 *Список кураторов:*\n"]
    for c in curators:
        lines.append(f"• {c.get('username', '—')} — {c.get('percent', '0%')}")
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')


# Команда /support
async def support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💬 *Поддержка:* @BlancSupport",
        parse_mode='Markdown'
    )


# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("👨‍💻 Панель воркера", url='https://7dice.icu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "🎉 Добро пожаловать в Blanc !\n\n"
        "💬 [Support](https://t.me/BlancSupport)\n"
        "📚 [Wiki](https://wiki.blanc.forum/)\n"
        "💬 [Chat](https://t.me/+_9wbP6M3nYhmOTRk)\n"
        "ℹ️ [Info](https://t.me/BlancCrypto)"
    )
    await update.message.reply_text(text, parse_mode='Markdown', reply_markup=reply_markup)

# Обработка кода от пользователя
async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    user = update.effective_user
    tg_id = user.id
    username = user.username or user.first_name

    # Загружаем активные коды
    pending_file = os.path.join(DATA_DIR, 'pending_codes.json')
    if not os.path.exists(pending_file):
        await update.message.reply_text("❌ Код недействителен или истек.")
        return

    with open(pending_file, 'r') as f:
        pending = json.load(f)

    if code not in pending:
        await update.message.reply_text("❌ Неверный код. Попробуйте снова.")
        return

    # Привязываем Telegram ID
    session_id = pending[code]["session"]
    users = load_json('users.json')
    if not isinstance(users, list):
        users = []

    # Ищем существующего пользователя по tg_id — если нашли, просто обновляем сессию
    # (сохраняем баланс, реквизиты и всё остальное)
    existing = None
    for u in users:
        if u.get('tg_id') == tg_id:
            existing = u
            break

    if existing:
        # Обновляем только сессию и username, всё остальное сохраняется
        existing['session']  = session_id
        existing['username'] = username
    else:
        # Новый пользователь
        users.append({
            'session':      session_id,
            'tg_id':        tg_id,
            'username':     username,
            'balance':      0,
            'total_profit': 0,
            'status':       'Novice worker',
            'percent':      70,
            'wallets': {
                'trc20': '',
                'erc20': '',
                'bep20': '',
                'ton':   '',
            }
        })

    save_json('users.json', users)

    # Удаляем использованный код
    del pending[code]
    with open(pending_file, 'w') as f:
        json.dump(pending, f)

    await update.message.reply_text(
        "✅ **Успешно!** Ваш аккаунт привязан.\n"
        "Перейдите на сайт и войдите в панель.\n\n"
        "📊 Панель: https://7dice.icu",
        parse_mode='Markdown'
    )

    # Уведомляем админа
    if ADMIN_ID:
        await context.bot.send_message(
            ADMIN_ID,
            f"🆕 **Новый воркер!**\n"
            f"👤 @{username}\n"
            f"🆔 ID: `{tg_id}`\n"
            f"🔑 Сессия: `{session_id}`",
            parse_mode='Markdown'
        )

# Обработка заявок на вывод (кнопки админа)
async def handle_withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split("_")
    action = data[0]  # approve, reject, done
    withdraw_id = int(data[1])

    withdrawals = load_json('withdrawals.json')
    if not isinstance(withdrawals, list):
        withdrawals = []

    withdraw = None
    for w in withdrawals:
        if w.get("id") == withdraw_id:
            withdraw = w
            break

    if not withdraw:
        await query.edit_message_text("❌ Заявка не найдена.")
        return

    users = load_json('users.json')
    if not isinstance(users, list):
        users = []

    user = next((u for u in users if u.get("tg_id") == withdraw["user_id"]), None)

    if action == "reject":
        withdraw["status"] = "rejected"
        if user:
            user["balance"] = user.get("balance", 0) + withdraw["amount"]
        await query.edit_message_text(f"❌ Заявка #{withdraw_id} отклонена.")

        if withdraw.get("user_id"):
            await context.bot.send_message(
                withdraw["user_id"],
                f"❌ Ваша заявка на вывод #{withdraw_id} отклонена.\n"
                f"Сумма {withdraw['amount']}$ возвращена на баланс."
            )

    elif action == "approve":
        withdraw["status"] = "in_progress"
        await query.edit_message_text(f"⏳ Заявка #{withdraw_id} принята в работу.")

        if withdraw.get("user_id"):
            await context.bot.send_message(
                withdraw["user_id"],
                f"✅ Заявка #{withdraw_id} принята в работу.\n"
                f"Сумма {withdraw['amount']}$ готовится к выплате."
            )

    elif action == "done":
        withdraw["status"] = "completed"
        if user:
            user["total_profit"] = user.get("total_profit", 0) + withdraw["amount"]
            # Пересчет статуса и процента
            if user["total_profit"] >= 25000:
                user["status"] = "Owner of work"
                user["percent"] = 80
            elif user["total_profit"] >= 10000:
                user["status"] = "Specialist in work"
                user["percent"] = 75
            else:
                user["status"] = "Novice worker"
                user["percent"] = 70

        await query.edit_message_text(f"✅ Заявка #{withdraw_id} выполнена.")

        if withdraw.get("user_id"):
            await context.bot.send_message(
                withdraw["user_id"],
                f"✅ Выплата #{withdraw_id} выполнена!\n"
                f"Сумма {withdraw['amount']}$ отправлена на ваши реквизиты."
            )

    save_json('users.json', users)
    save_json('withdrawals.json', withdrawals)

# Команда /givebalance <сессия> <сумма> — только для админа
async def give_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Использование: /givebalance <сессия> <сумма>\nПример: /givebalance 8eecca9a5b823d78 100")
        return
    session_id = args[0].strip()
    try:
        amount = float(args[1].replace('$', '').replace(',', '.').strip())
    except ValueError:
        await update.message.reply_text("❌ Неверная сумма. Используй число, например: 100")
        return

    users = load_json('users.json')
    if not isinstance(users, list):
        await update.message.reply_text("❌ Ошибка чтения данных.")
        return

    for u in users:
        if u.get('session') == session_id:
            u['balance'] = round(u.get('balance', 0) + amount, 2)
            save_json('users.json', users)
            await update.message.reply_text(
                f"✅ Баланс начислен!\n"
                f"👤 @{u.get('username', '?')} (ID: {u.get('tg_id', '?')})\n"
                f"➕ +{amount}$\n"
                f"💰 Новый баланс: {u['balance']}$"
            )
            return

    await update.message.reply_text(f"❌ Пользователь с сессией `{session_id}` не найден.", parse_mode='Markdown')


def main():
    # Создаем Application с прокси
    if PROXY_URL:
        from telegram.request import HTTPXRequest
        request = HTTPXRequest(proxy=PROXY_URL)
        application = Application.builder().token(TOKEN).request(request).build()
        print(f"🌐 Используется прокси: {PROXY_URL}")
    else:
        application = Application.builder().token(TOKEN).build()
        print("🌐 Прямое подключение (без прокси)")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("givebalance", give_balance))
    application.add_handler(CommandHandler("curator", curator_cmd))
    application.add_handler(CommandHandler("support", support_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code))
    application.add_handler(CallbackQueryHandler(handle_withdraw_callback, pattern="^(approve|reject|done)_"))

    print("🤖 Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()