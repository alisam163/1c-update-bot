import os
import sys
import json
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler,
    CallbackQueryHandler, filters, ConversationHandler
)

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

SET_VERSION = 1

# --- НАСТРОЙКИ ---
USERNAME = os.getenv("1C_USERNAME")
PASSWORD = os.getenv("1C_PASSWORD")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
CHECK_INTERVAL_HOURS = int(os.getenv("CHECK_INTERVAL_HOURS", "1"))

# Читаем список ID из .env (через запятую)
admin_ids_str = os.getenv("TG_ADMIN_ID", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]

if not all([USERNAME, PASSWORD, TG_BOT_TOKEN]) or not ADMIN_IDS:
    print("КРИТИЧЕСКАЯ ОШИБКА: Не заданы обязательные переменные окружения (проверьте .env)!")
    sys.exit(1)

DATA_DIR = "/app/data"
CONFIG_FILE = f"{DATA_DIR}/subscriptions_v2.json"
RELEASES_BASE = "https://releases.1c.ru"


# --- БАЗА ДАННЫХ ---
def load_db():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_db(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def extract_nick(url_or_text):
    url_or_text = (url_or_text or "").strip()
    if "releases.1c.ru/project/" in url_or_text:
        parsed = urlparse(url_or_text)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) >= 2 and parts[-2] == "project":
            return parts[-1]
        if len(parts) >= 1 and parts[0] == "project" and len(parts) >= 2:
            return parts[1]
    return url_or_text


# --- 1С ---
def get_authenticated_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Referer": "https://login.1c.ru/login?service=https%3A%2F%2Freleases.1c.ru%2Fpublic%2Fsecurity_check"
    })
    try:
        login_page_url = "https://login.1c.ru/login?service=https%3A%2F%2Freleases.1c.ru%2Fpublic%2Fsecurity_check"
        response = session.get(login_page_url, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        form = soup.find("form", id="loginForm") or soup.find("form")
        if not form: return session
        
        payload = {}
        for hidden_input in form.find_all("input", type="hidden"):
            if hidden_input.get("name"):
                payload[hidden_input.get("name")] = hidden_input.get("value") or ""
        
        payload["username"] = USERNAME
        payload["password"] = PASSWORD
        payload["_eventId"] = "submit"
        
        action = form.get("action", "")
        login_url = ("https://login.1c.ru" + action) if action.startswith("/") else action
        if not login_url.startswith("http"):
            login_url = "https://login.1c.ru/login?service=https%3A%2F%2Freleases.1c.ru%2Fpublic%2Fsecurity_check"
        
        session.post(login_url, data=payload, timeout=15, allow_redirects=True)
        return session
    except Exception as e:
        logging.error(f"Ошибка при авторизации: {e}")
        return session

def get_latest_site_version(nick, session):
    project_url = f"{RELEASES_BASE}/project/{nick}"
    try:
        response = session.get(project_url, timeout=15)
        if response.status_code != 200: return None
        soup = BeautifulSoup(response.text, 'html.parser')
        tables = soup.find_all('table')
        for table in tables:
            for row in table.find_all('tr'):
                cols = row.find_all('td')
                if len(cols) >= 2:
                    version_text = cols[0].get_text(strip=True)
                    if version_text and version_text[0].isdigit() and version_text.count('.') >= 2:
                        return version_text
    except Exception as e:
        logging.error(f"Ошибка запроса к 1С для {nick}: {e}")
    return None


# --- ФОНОВАЯ ПРОВЕРКА ---
async def cron_check_job(context: ContextTypes.DEFAULT_TYPE):
    db = load_db()
    if not db: return

    session = get_authenticated_session()
    site_versions_cache = {}
    
    # Собираем все уникальные конфигурации из базы
    all_products = set()
    for chat_id, products in db.items():
        if chat_id == "_subscribers": continue
        all_products.update(products.keys())

    # Запрашиваем версии с сайта
    for nick in all_products:
        site_versions_cache[nick] = get_latest_site_version(nick, session)

    # Работаем только с администраторами из .env
    for chat_id in ADMIN_IDS:
        str_chat_id = str(chat_id)
        if str_chat_id not in db:
            continue
            
        products = db[str_chat_id]
        for nick, info in products.items():
            current_site_version = site_versions_cache.get(nick)
            if not current_site_version: continue

            my_version = info.get("my_version", "")
            old_site_version = info.get("site_version", "")

            if my_version != current_site_version:
                if current_site_version != old_site_version:
                    title = f"🚀 **Вышел новый релиз 1С ({nick})!**\n\n"
                    db[str_chat_id][nick]["site_version"] = current_site_version
                    save_db(db)
                else:
                    title = f"🔔 **Напоминание об обновлении ({nick})**\n\n"

                keyboard = [[InlineKeyboardButton("📥 Я установил эту версию", callback_data=f"installed_ok:{nick}:{current_site_version}")]]
                msg = (
                    f"{title}Версия на сайте: `{current_site_version}`\n"
                    f"Ваша текущая версия: `{my_version}`\n\n"
                    f"[Страница загрузки]({RELEASES_BASE}/project/{nick})"
                )
                try:
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
                except Exception as e:
                    logging.error(f"Ошибка отправки в чат {chat_id}: {e}")


# --- МЕНЮ ---
def get_main_menu():
    keyboard = [
        [InlineKeyboardButton("📋 Мои подписки", callback_data="menu_list")],
        [InlineKeyboardButton("➕ Добавить конфигурацию", callback_data="menu_add")],
        [InlineKeyboardButton("❌ Удалить подписку", callback_data="menu_remove")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Привет! Я бот для контроля обновлений 1С.\n\n"
        "Я слежу за выходом новых релизов и предупреждаю вас об этом.\n"
        "Выберите действие в меню ниже:"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=get_main_menu())
    else:
        await update.callback_query.edit_message_text(text, reply_markup=get_main_menu())


# --- ОБРАБОТКА МЕНЮ ---
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat_id)
    data = query.data

    if data == "menu_list":
        db = load_db()
        user_subs = db.get(chat_id, {})
        if not user_subs:
            await query.edit_message_text("📭 У вас пока нет активных подписок.", reply_markup=get_main_menu())
            return
        
        msg = "📋 **Ваши подписки:**\n\n"
        for nick, info in user_subs.items():
            status = "🟢 Актуально" if info['my_version'] == info['site_version'] else "🔴 Требует обновления"
            msg += f"• `{nick}`\n  У вас: `{info['my_version']}` | На сайте: `{info['site_version']}`\n  Статус: {status}\n\n"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_main_menu())

    elif data == "menu_add":
        await query.edit_message_text(
            "➕ Отправьте мне ссылку на конфигурацию 1С.\n"
            "Пример: `https://releases.1c.ru/project/AutoService30`\n\n"
            "(Или просто напишите имя, например `AutoService30`)",
            parse_mode="Markdown",
            reply_markup=get_main_menu()
        )

    elif data == "menu_remove":
        db = load_db()
        user_subs = db.get(chat_id, {})
        if not user_subs:
            await query.edit_message_text("📭 Нечего удалять, у вас нет подписок.", reply_markup=get_main_menu())
            return
        
        keyboard = []
        for nick in user_subs.keys():
            keyboard.append([InlineKeyboardButton(f"🗑 Удалить {nick}", callback_data=f"del_{nick}")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu_back")])
        await query.edit_message_text("Выберите, что удалить:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("del_"):
        nick = data.replace("del_", "")
        db = load_db()
        if chat_id in db and nick in db[chat_id]:
            del db[chat_id][nick]
            if not db[chat_id]: del db[chat_id]
            save_db(db)
        await query.edit_message_text(f"🗑 Подписка на `{nick}` удалена.", parse_mode="Markdown", reply_markup=get_main_menu())

    elif data == "menu_back":
        await start(update, context)


# --- ДИАЛОГ ДОБАВЛЕНИЯ КОНФИГУРАЦИИ ---
async def start_add_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else update.message.text
    text = text.strip()
    chat_id = str(update.message.chat_id)

    nick = extract_nick(text)
    if not nick or len(nick) < 3 or "/" in nick or " " in nick:
        await update.message.reply_text("❌ Не удалось распознать конфигурацию. Пришлите корректную ссылку.", reply_markup=get_main_menu())
        return ConversationHandler.END

    context.user_data["current_nick"] = nick
    await update.message.reply_text(
        f"🔍 Продукт **{nick}** распознан.\n\n"
        f"✍️ Напишите, какая версия у вас **сейчас установлена** в базе? (например: `3.0.14.115`):",
        parse_mode="Markdown"
    )
    return SET_VERSION

async def save_version_and_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    my_version = update.message.text.strip()
    nick = context.user_data.get("current_nick")
    chat_id = str(update.message.chat_id)

    if not nick:
        await update.message.reply_text("❌ Ошибка.", reply_markup=get_main_menu())
        return ConversationHandler.END

    await update.message.reply_text("🔄 Проверяю актуальную версию...")
    session = get_authenticated_session()
    site_version = get_latest_site_version(nick, session) or my_version

    db = load_db()
    if chat_id not in db: db[chat_id] = {}
    db[chat_id][nick] = {"site_version": site_version, "my_version": my_version}
    save_db(db)

    status_msg = "🟢 У вас актуальная версия!" if my_version == site_version else "🔴 На сайте доступна новая версия!"
    
    await update.message.reply_text(
        f"✅ Успешно!\n\n📦 `{nick}`\n🛠 Ваша версия: `{my_version}`\n🌐 На сайте: `{site_version}`\nСтатус: {status_msg}",
        parse_mode="Markdown",
        reply_markup=get_main_menu()
    )
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Действие отменено.", reply_markup=get_main_menu())
    return ConversationHandler.END


# --- КНОПКИ ОБНОВЛЕНИЙ ---
async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = str(query.message.chat_id)

    if data.startswith("installed_ok:"):
        _, nick, version = data.split(":", 2)
        db = load_db()
        if chat_id in db and nick in db[chat_id]:
            db[chat_id][nick]["my_version"] = version
            db[chat_id][nick]["site_version"] = version
            save_db(db)
            await query.edit_message_text(
                text=f"🎉 **Отлично!** Зафиксировано, что для `{nick}` установлена версия `{version}`.",
                parse_mode="Markdown"
            )

def main():
    application = Application.builder().token(TG_BOT_TOKEN).build()

    add_conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("add", start_add_subscription),
            MessageHandler(filters.TEXT & ~filters.COMMAND, start_add_subscription)
        ],
        states={
            SET_VERSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_version_and_finish)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=False
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern="^(menu_|del_)"))
    application.add_handler(CallbackQueryHandler(button_callback_handler, pattern="^installed_ok:"))
    application.add_handler(add_conv_handler)

    application.job_queue.run_repeating(cron_check_job, interval=CHECK_INTERVAL_HOURS * 3600, first=15)

    logging.info("Бот запущен. Меню готово.")
    application.run_polling()

if __name__ == '__main__':
    main()