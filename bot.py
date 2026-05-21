#!/usr/bin/env python3
import logging, random, sqlite3, os, threading, time
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [5207853162, 5406117718]
CHANNEL_ID = -1002376241083

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== БАЗА ДАННЫХ ==========
# Используем переменную DB_PATH, если она задана (например, /app/data/giveaway.db)
DB_PATH = os.getenv("DB_PATH", "giveaway.db")
# Создаём папку, если нужно
db_dir = os.path.dirname(DB_PATH)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)
    logger.info(f"Created directory {db_dir}")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        joined_date TEXT,
        is_verified INTEGER DEFAULT 0,
        verification_date TEXT
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS giveaways (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        description TEXT,
        winner_count INTEGER,
        start_date TEXT,
        end_date TEXT,
        is_active INTEGER,
        channel_id TEXT,
        require_subscription INTEGER,
        message_id INTEGER
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        giveaway_id INTEGER,
        user_id INTEGER,
        join_date TEXT,
        PRIMARY KEY (giveaway_id, user_id)
    )
""")
conn.commit()
logger.info(f"Database initialized at {DB_PATH}")

def add_user(user_id, username, first_name):
    cur.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, joined_date, is_verified) VALUES (?, ?, ?, ?, 0)",
                (user_id, username, first_name, datetime.now().isoformat()))
    conn.commit()
    logger.info(f"User {user_id} added/updated")

def verify_user(user_id):
    cur.execute("UPDATE users SET is_verified = 1, verification_date = ? WHERE user_id = ?",
                (datetime.now().isoformat(), user_id))
    conn.commit()
    success = cur.rowcount > 0
    logger.info(f"verify_user({user_id}) -> {success}")
    return success

def is_verified(user_id):
    cur.execute("SELECT is_verified FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    verified = row and row[0] == 1
    logger.info(f"is_verified({user_id}) -> {verified}")
    return verified

def add_participant(giveaway_id, user_id):
    try:
        cur.execute("INSERT INTO participants (giveaway_id, user_id, join_date) VALUES (?, ?, ?)",
                    (giveaway_id, user_id, datetime.now().isoformat()))
        conn.commit()
        logger.info(f"User {user_id} joined giveaway {giveaway_id}")
        return True
    except Exception as e:
        logger.info(f"User {user_id} already in giveaway {giveaway_id}: {e}")
        return False

def get_giveaway_info(giveaway_id):
    cur.execute("SELECT id, name, description, winner_count, start_date, end_date, is_active, channel_id, require_subscription FROM giveaways WHERE id = ?", (giveaway_id,))
    return cur.fetchone()

def create_giveaway(name, description, winners, hours, require_sub):
    start = datetime.now()
    end = start + timedelta(hours=hours)
    cur.execute("""
        INSERT INTO giveaways (name, description, winner_count, start_date, end_date, is_active, channel_id, require_subscription)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
    """, (name, description, winners, start.isoformat(), end.isoformat(), str(CHANNEL_ID), 1 if require_sub else 0))
    conn.commit()
    return cur.lastrowid

def get_active_giveaways():
    cur.execute("SELECT id, name, winner_count, end_date, require_subscription FROM giveaways WHERE is_active = 1 ORDER BY end_date")
    return cur.fetchall() or []

def get_participants_count(gid):
    cur.execute("SELECT COUNT(*) FROM participants WHERE giveaway_id = ?", (gid,))
    return cur.fetchone()[0]

def get_participants(gid):
    cur.execute("SELECT user_id FROM participants WHERE giveaway_id = ?", (gid,))
    return [row[0] for row in cur.fetchall()]

# ========== КАПЧА ==========
captcha_storage = {}
def generate_captcha():
    a, b = random.randint(1,10), random.randint(1,10)
    op = random.choice(['+','-','*'])
    if op == '+': return f"{a} + {b}", str(a+b)
    if op == '-': return f"{a} - {b}", str(a-b)
    return f"{a} x {b}", str(a*b)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def is_admin(user_id):
    return user_id in ADMIN_IDS

def check_subscription(bot, user_id, channel_id):
    try:
        member = bot.get_chat_member(channel_id, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

def format_time_left(end_date):
    try:
        diff = datetime.fromisoformat(end_date) - datetime.now()
        if diff.total_seconds() <= 0: return "Завершен"
        days, hours, minutes = diff.days, diff.seconds//3600, (diff.seconds%3600)//60
        if days > 0: return f"{days}д {hours}ч"
        if hours > 0: return f"{hours}ч {minutes}мин"
        return f"{minutes}мин"
    except:
        return "Неизвестно"

# ========== ОБРАБОТЧИКИ КОМАНД ==========
def start(update, context):
    user = update.effective_user
    add_user(user.id, user.username or "", user.first_name or "")
    keyboard = []
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("Админ-панель", callback_data="admin")])
    keyboard.append([InlineKeyboardButton("Пройти проверку", callback_data="verify")])
    update.message.reply_text(f"Привет, {user.first_name}!", reply_markup=InlineKeyboardMarkup(keyboard))

def verify(update, context):
    if update.callback_query:
        msg = update.callback_query.message
        uid = update.effective_user.id
        update.callback_query.answer()
    else:
        msg = update.message
        uid = update.effective_user.id
    if is_verified(uid):
        msg.reply_text("✅ Вы уже верифицированы!")
        return
    q, a = generate_captcha()
    captcha_storage[uid] = {"answer": a, "time": datetime.now(), "attempts": 0}
    msg.reply_text(f"🔐 Решите: {q} = ? (отправьте число)")

def handle_text(update, context):
    uid = update.effective_user.id
    text = update.message.text.strip()
    if uid in captcha_storage:
        cap = captcha_storage[uid]
        if datetime.now() - cap["time"] > timedelta(minutes=5):
            update.message.reply_text("⏰ Время вышло. Начните заново /start")
            del captcha_storage[uid]
            return
        if text == cap["answer"]:
            if verify_user(uid):
                del captcha_storage[uid]
                update.message.reply_text("✅ Проверка пройдена! Теперь вы можете участвовать в розыгрышах.")
            else:
                update.message.reply_text("❌ Ошибка, попробуйте снова /start")
        else:
            cap["attempts"] += 1
            if cap["attempts"] >= 3:
                update.message.reply_text("❌ Попытки кончились. Начните заново /start")
                del captcha_storage[uid]
            else:
                update.message.reply_text(f"❌ Неверно, осталось {3 - cap['attempts']} попыток")

def new_giveaway(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    args = context.args
    if len(args) < 2:
        update.message.reply_text("Использование: /new <название> <победителей> [часы] [описание] [sub]")
        return
    name = args[0]
    winners = int(args[1])
    hours = int(args[2]) if len(args) > 2 and args[2].isdigit() else 24
    require_sub = 1 if "sub" in args else 0
    desc = " ".join(args[3:]) if len(args) > 3 else "Розыгрыш"
    gid = create_giveaway(name, desc, winners, hours, require_sub)
    end_date = datetime.now() + timedelta(hours=hours)
    text = f"🎁 НОВЫЙ РОЗЫГРЫШ!\n{name}\n{desc}\n👥 Победителей: {winners}\n⏰ Завершится: {end_date.strftime('%d.%m.%Y %H:%M')}"
    if require_sub:
        text += "\n⚠️ Требуется подписка на канал"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Участвовать", callback_data=f"join_{gid}")]])
    context.bot.send_message(chat_id=CHANNEL_ID, text=text, reply_markup=markup)
    update.message.reply_text(f"✅ Розыгрыш #{gid} создан!")

def button_handler(update, context):
    query = update.callback_query
    query.answer()
    data = query.data
    if data == "admin":
        if is_admin(query.from_user.id):
            query.edit_message_text("👑 Админ-панель\n/new <название> <победителей> [часы] [описание] [sub]")
        else:
            query.edit_message_text("Нет прав")
    elif data == "verify":
        verify(update, context)
    elif data.startswith("join_"):
        gid = int(data.split("_")[1])
        uid = query.from_user.id
        if not is_verified(uid):
            query.answer("🔐 Сначала пройдите проверку!", show_alert=True)
            return
        info = get_giveaway_info(gid)
        if not info or info[6] != 1:
            query.answer("❌ Розыгрыш не найден или завершён", show_alert=True)
            return
        require_sub = info[8] if len(info) > 8 else 0
        if require_sub and not check_subscription(context.bot, uid, CHANNEL_ID):
            query.answer("📢 Подпишитесь на канал, чтобы участвовать", show_alert=True)
            return
        if add_participant(gid, uid):
            query.answer("✅ Вы участвуете!", show_alert=True)
        else:
            query.answer("ℹ️ Вы уже участвуете", show_alert=True)

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("new", new_giveaway))
    dp.add_handler(CallbackQueryHandler(button_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
