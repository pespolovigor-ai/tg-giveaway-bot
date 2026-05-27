#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import random
import sqlite3
from datetime import datetime, timedelta
import os
import hashlib
import threading
import time
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext

# ========== КОНФИГУРАЦИЯ ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = [5207853162, 5406117718]
CHANNEL_ID = "@giveayboty"
SOS_PASSWORD = os.getenv("SOS_PASSWORD", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not SOS_PASSWORD:
    logger = logging.getLogger(__name__)
    logger.warning("SOS_PASSWORD not set, SOS commands will not work")

# ========== НАСТРОЙКА БАЗЫ ДАННЫХ ==========
DB_PATH = os.getenv("DB_PATH", "giveaway.db")
db_dir = os.path.dirname(DB_PATH)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== SOS MODE ==========
sos_mode = False
sos_lock = threading.Lock()

def activate_sos():
    global sos_mode
    with sos_lock:
        sos_mode = True
        logger.warning("!!! SOS MODE ACTIVATED !!!")

def deactivate_sos():
    global sos_mode
    with sos_lock:
        sos_mode = False
        logger.warning("!!! SOS MODE DEACTIVATED !!!")

def is_sos_active():
    return sos_mode

# ========== RATE LIMITING ==========
rate_limit_storage = defaultdict(list)
RATE_LIMIT_SECONDS = 5
RATE_LIMIT_CALLS = 3

def check_rate_limit(user_id):
    if user_id in ADMIN_IDS:
        return True
    now = time.time()
    timestamps = rate_limit_storage[user_id]
    while timestamps and timestamps[0] < now - RATE_LIMIT_SECONDS:
        timestamps.pop(0)
    if len(timestamps) >= RATE_LIMIT_CALLS:
        return False
    timestamps.append(now)
    return True

# ========== ХРАНИЛИЩЕ ВЕРИФИКАЦИИ ==========
VERIFIED_FILE = os.path.join(os.path.dirname(DB_PATH), "verified_users.txt")

def load_verified():
    verified = set()
    if os.path.exists(VERIFIED_FILE):
        with open(VERIFIED_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    verified.add(int(line))
    logger.info(f"Загружено {len(verified)} верифицированных пользователей")
    return verified

def save_verified(verified_set):
    with open(VERIFIED_FILE, "w") as f:
        for uid in verified_set:
            f.write(f"{uid}\n")

verified_users = load_verified()

def set_verified(user_id):
    if user_id not in verified_users:
        verified_users.add(user_id)
        save_verified(verified_users)
        logger.info(f"User {user_id} verified")
        return True
    return False

def is_verified(user_id):
    return user_id in verified_users

# ========== SQLite ==========
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        joined_date TEXT NOT NULL,
        is_verified INTEGER DEFAULT 0,
        verification_date TEXT,
        verification_method TEXT,
        is_banned INTEGER DEFAULT 0,
        ban_reason TEXT,
        banned_date TEXT,
        ip_hash TEXT,
        last_activity TEXT,
        verification_attempts INTEGER DEFAULT 0
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS verification_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        verification_type TEXT NOT NULL,
        success INTEGER NOT NULL,
        attempt_date TEXT NOT NULL,
        ip_hash TEXT
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS ban_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        admin_id INTEGER,
        reason TEXT,
        ban_date TEXT NOT NULL,
        unban_date TEXT
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS ip_addresses (
        ip_hash TEXT PRIMARY KEY,
        user_count INTEGER DEFAULT 1,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS giveaways (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        winner_count INTEGER DEFAULT 1,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        message_id INTEGER,
        channel_id TEXT,
        auto_finish INTEGER DEFAULT 1,
        require_subscription INTEGER DEFAULT 0
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        giveaway_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        join_date TEXT NOT NULL,
        is_valid INTEGER DEFAULT 1,
        referred_by INTEGER,
        bonus_entries INTEGER DEFAULT 0,
        PRIMARY KEY (giveaway_id, user_id)
    )
""")
cur.execute("""
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER NOT NULL,
        referred_id INTEGER NOT NULL,
        giveaway_id INTEGER NOT NULL,
        referral_date TEXT NOT NULL,
        UNIQUE(referrer_id, referred_id, giveaway_id)
    )
""")
conn.commit()

class Database:
    def __init__(self):
        self.lock = threading.Lock()

    def _execute(self, query, params=(), fetchone=False, fetchall=False, commit=False):
        with self.lock:
            cur.execute(query, params)
            result = None
            if fetchone:
                result = cur.fetchone()
            elif fetchall:
                result = cur.fetchall()
            if commit:
                conn.commit()
            return result

    def add_user(self, user_id, username, first_name, last_name=""):
        try:
            current_time = datetime.now().isoformat()
            exists = self._execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
            with self.lock:
                if exists:
                    cur.execute("UPDATE users SET username=?, first_name=?, last_name=?, last_activity=? WHERE user_id=?", 
                                (username, first_name, last_name, current_time, user_id))
                else:
                    cur.execute("INSERT INTO users (user_id, username, first_name, last_name, joined_date, last_activity, is_verified) VALUES (?,?,?,?,?,?,0)",
                                (user_id, username, first_name, last_name, current_time, current_time))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"add_user error: {e}")
            return False

    def verify_user(self, user_id, method="captcha", ip_hash=None):
        try:
            current_time = datetime.now().isoformat()
            with self.lock:
                cur.execute("UPDATE users SET is_verified=1, verification_date=?, verification_method=?, verification_attempts=verification_attempts+1 WHERE user_id=?", 
                            (current_time, method, user_id))
                cur.execute("INSERT INTO verification_history (user_id, verification_type, success, attempt_date, ip_hash) VALUES (?,?,1,?,?)",
                            (user_id, method, current_time, ip_hash))
                conn.commit()
            set_verified(user_id)
            return True
        except Exception as e:
            logger.error(f"verify_user error: {e}")
            conn.rollback()
            return False

    def is_verified(self, user_id):
        return is_verified(user_id)

    def record_verification_attempt(self, user_id, success=False, method="captcha", ip_hash=None):
        try:
            current_time = datetime.now().isoformat()
            with self.lock:
                cur.execute("UPDATE users SET verification_attempts = verification_attempts + 1 WHERE user_id=?", (user_id,))
                cur.execute("INSERT INTO verification_history (user_id, verification_type, success, attempt_date, ip_hash) VALUES (?,?,?,?,?)",
                            (user_id, method, 1 if success else 0, current_time, ip_hash))
                conn.commit()
            return True
        except Exception:
            return False

    def update_user_activity(self, user_id):
        try:
            self._execute("UPDATE users SET last_activity=? WHERE user_id=?", (datetime.now().isoformat(), user_id), commit=True)
        except Exception:
            pass

    def ban_user(self, user_id, admin_id, reason="Нарушение", days=30):
        try:
            current_time = datetime.now()
            unban_date = current_time + timedelta(days=days)
            with self.lock:
                cur.execute("INSERT INTO ban_list (user_id, admin_id, reason, ban_date, unban_date) VALUES (?,?,?,?,?)",
                            (user_id, admin_id, reason, current_time.isoformat(), unban_date.isoformat()))
                cur.execute("UPDATE users SET is_banned=1, ban_reason=?, banned_date=? WHERE user_id=?", (reason, current_time.isoformat(), user_id))
                cur.execute("UPDATE participants SET is_valid=0 WHERE user_id=?", (user_id,))
                conn.commit()
            return True
        except Exception:
            return False

    def unban_user(self, user_id):
        try:
            self._execute("UPDATE users SET is_banned=0, ban_reason=NULL, banned_date=NULL WHERE user_id=?", (user_id,), commit=True)
            return True
        except Exception:
            return False

    def is_banned(self, user_id):
        try:
            result = self._execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,), fetchone=True)
            return bool(result and result[0] == 1)
        except Exception:
            return False

    def get_banned_users(self):
        try:
            return self._execute("SELECT user_id, username, first_name, ban_reason, banned_date FROM users WHERE is_banned=1 ORDER BY banned_date DESC", fetchall=True) or []
        except Exception:
            return []

    def add_ip_address(self, user_id, ip_seed):
        try:
            ip_hash = hashlib.sha256(ip_seed.encode()).hexdigest()[:32]
            current_time = datetime.now().isoformat()
            with self.lock:
                cur.execute("UPDATE users SET ip_hash=? WHERE user_id=?", (ip_hash, user_id))
                cur.execute("SELECT user_count FROM ip_addresses WHERE ip_hash=?", (ip_hash,))
                exists = cur.fetchone()
                if exists:
                    cur.execute("UPDATE ip_addresses SET user_count=user_count+1, last_seen=? WHERE ip_hash=?", (current_time, ip_hash))
                else:
                    cur.execute("INSERT INTO ip_addresses (ip_hash, user_count, first_seen, last_seen) VALUES (?,1,?,?)", (ip_hash, current_time, current_time))
                conn.commit()
            return ip_hash
        except Exception:
            return None

    def get_suspicious_ips(self, threshold=2):
        try:
            return self._execute("SELECT ip_hash, user_count, last_seen FROM ip_addresses WHERE user_count>=? ORDER BY user_count DESC", (threshold,), fetchall=True) or []
        except Exception:
            return []

    def get_users_by_ip(self, ip_hash):
        try:
            return self._execute("SELECT user_id, username, first_name, joined_date FROM users WHERE ip_hash=? ORDER BY joined_date", (ip_hash,), fetchall=True) or []
        except Exception:
            return []

    def check_multiple_accounts(self, user_id):
        try:
            result = self._execute("SELECT ip_hash FROM users WHERE user_id=?", (user_id,), fetchone=True)
            if not result or not result[0]:
                return []
            ip_hash = result[0]
            rows = self._execute("SELECT user_id FROM users WHERE ip_hash=? AND user_id!=?", (ip_hash, user_id), fetchall=True) or []
            return [row[0] for row in rows]
        except Exception:
            return []

    def create_giveaway(self, name, description, winners, hours, channel_id, require_sub=0):
        try:
            start_date = datetime.now()
            end_date = start_date + timedelta(hours=hours)
            with self.lock:
                cur.execute("INSERT INTO giveaways (name, description, winner_count, start_date, end_date, is_active, channel_id, auto_finish, require_subscription) VALUES (?,?,?,?,?,1,?,1,?)",
                            (name, description, winners, start_date.isoformat(), end_date.isoformat(), channel_id, require_sub))
                conn.commit()
                return cur.lastrowid
        except Exception:
            return None

    def update_message_id(self, giveaway_id, message_id):
        try:
            self._execute("UPDATE giveaways SET message_id=? WHERE id=?", (message_id, giveaway_id), commit=True)
        except Exception:
            pass

    def add_participant(self, giveaway_id, user_id, referred_by=None):
        try:
            current_time = datetime.now().isoformat()
            with self.lock:
                cur.execute("INSERT INTO participants (giveaway_id, user_id, join_date, referred_by) VALUES (?,?,?,?)", (giveaway_id, user_id, current_time, referred_by))
                if referred_by:
                    try:
                        cur.execute("INSERT INTO referrals (referrer_id, referred_id, giveaway_id, referral_date) VALUES (?,?,?,?)", (referred_by, user_id, giveaway_id, current_time))
                        cur.execute("UPDATE participants SET bonus_entries = bonus_entries + 1 WHERE giveaway_id=? AND user_id=?", (giveaway_id, referred_by))
                    except Exception:
                        pass
                conn.commit()
            return True
        except Exception:
            return False

    def get_referral_count(self, user_id, giveaway_id):
        try:
            result = self._execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND giveaway_id=?", (user_id, giveaway_id), fetchone=True)
            return result[0] if result else 0
        except Exception:
            return 0

    def get_bonus_entries(self, user_id, giveaway_id):
        try:
            result = self._execute("SELECT bonus_entries FROM participants WHERE giveaway_id=? AND user_id=?", (giveaway_id, user_id), fetchone=True)
            return result[0] if result else 0
        except Exception:
            return 0

    def get_top_referrers(self, limit=10):
        try:
            return self._execute("SELECT r.referrer_id, u.username, u.first_name, COUNT(r.referred_id) AS ref_count FROM referrals r LEFT JOIN users u ON r.referrer_id = u.user_id GROUP BY r.referrer_id ORDER BY ref_count DESC LIMIT ?", (limit,), fetchall=True) or []
        except Exception:
            return []

    def remove_participant(self, giveaway_id, user_id):
        try:
            self._execute("UPDATE participants SET is_valid=0 WHERE giveaway_id=? AND user_id=?", (giveaway_id, user_id), commit=True)
            return True
        except Exception:
            return False

    def get_active_giveaways(self):
        try:
            return self._execute("SELECT id, name, winner_count, end_date, require_subscription FROM giveaways WHERE is_active=1 ORDER BY end_date", fetchall=True) or []
        except Exception:
            return []

    def get_giveaway_info(self, giveaway_id):
        try:
            return self._execute("SELECT id, name, description, winner_count, start_date, end_date, is_active, message_id, channel_id, auto_finish, require_subscription FROM giveaways WHERE id=?", (giveaway_id,), fetchone=True)
        except Exception:
            return None

    def get_participants(self, giveaway_id, valid_only=True):
        try:
            if valid_only:
                rows = self._execute("SELECT user_id FROM participants WHERE giveaway_id=? AND is_valid=1", (giveaway_id,), fetchall=True) or []
            else:
                rows = self._execute("SELECT user_id FROM participants WHERE giveaway_id=?", (giveaway_id,), fetchall=True) or []
            return [row[0] for row in rows]
        except Exception:
            return []

    def get_participants_with_info(self, giveaway_id):
        try:
            return self._execute("SELECT p.user_id, u.username, u.first_name, u.is_banned, p.join_date FROM participants p LEFT JOIN users u ON p.user_id = u.user_id WHERE p.giveaway_id=? AND p.is_valid=1 ORDER BY p.join_date", (giveaway_id,), fetchall=True) or []
        except Exception:
            return []

    def get_participants_count(self, giveaway_id):
        try:
            result = self._execute("SELECT COUNT(*) FROM participants WHERE giveaway_id=? AND is_valid=1", (giveaway_id,), fetchone=True)
            return result[0] if result else 0
        except Exception:
            return 0

    def end_giveaway(self, giveaway_id):
        try:
            self._execute("UPDATE giveaways SET is_active=0 WHERE id=?", (giveaway_id,), commit=True)
            return True
        except Exception:
            return False

    def get_giveaways_to_finish(self):
        try:
            current_time = datetime.now().isoformat()
            rows = self._execute("SELECT id FROM giveaways WHERE is_active=1 AND auto_finish=1 AND end_date<=?", (current_time,), fetchall=True) or []
            return [row[0] for row in rows]
        except Exception:
            return []

    def get_verification_info(self, user_id):
        try:
            return self._execute("SELECT is_verified, verification_date, verification_method, verification_attempts FROM users WHERE user_id=?", (user_id,), fetchone=True)
        except Exception:
            return None

    # ========== SOS методы ==========
    def ban_all_users(self, except_admins=True):
        try:
            current_time = datetime.now().isoformat()
            with self.lock:
                if except_admins:
                    placeholders = ','.join(['?'] * len(ADMIN_IDS))
                    cur.execute(f"UPDATE users SET is_banned=1, ban_reason='Аварийный режим SOS', banned_date=? WHERE user_id NOT IN ({placeholders})", [current_time] + ADMIN_IDS)
                else:
                    cur.execute("UPDATE users SET is_banned=1, ban_reason='Аварийный режим SOS', banned_date=?", (current_time,))
                cur.execute("UPDATE participants SET is_valid=0")
                conn.commit()
            logger.warning("Бан всех пользователей выполнен")
            return True
        except Exception as e:
            logger.error(f"ban_all_users error: {e}")
            return False

    def finish_all_active_giveaways(self):
        try:
            active = self.get_active_giveaways()
            for g in active:
                self.end_giveaway(g[0])
            logger.warning(f"Завершено {len(active)} розыгрышей")
            return len(active)
        except Exception as e:
            logger.error(f"finish_all_active_giveaways error: {e}")
            return 0

db = Database()
captcha_storage = {}

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def generate_captcha():
    a, b = random.randint(1,10), random.randint(1,10)
    op = random.choice(['+','-','*'])
    if op == '+': return f"{a} + {b}", str(a+b)
    if op == '-': return f"{a} - {b}", str(a-b)
    return f"{a} x {b}", str(a*b)

def extract_ip_seed(update):
    user = update.effective_user
    return f"{user.id}.{hash(str(user.id)) % 255}.{hash(user.username or '') % 255}"

def is_admin(user_id):
    return user_id in ADMIN_IDS

def check_subscription(bot, user_id, channel_id):
    try:
        member = bot.get_chat_member(channel_id, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

def format_time_left(end_date):
    try:
        diff = datetime.fromisoformat(end_date) - datetime.now()
        if diff.total_seconds() <= 0: return "Завершен"
        days, hours, minutes = diff.days, diff.seconds//3600, (diff.seconds%3600)//60
        if days > 0: return f"{days}д {hours}ч"
        if hours > 0: return f"{hours}ч {minutes}мин"
        return f"{minutes}мин"
    except Exception:
        return "Неизвестно"

def create_progress_bar(start_date, end_date, length=10):
    try:
        start, end = datetime.fromisoformat(start_date), datetime.fromisoformat(end_date)
        now = datetime.now()
        total = (end-start).total_seconds()
        if total <= 0: return "[" + "█" * length + "]"
        elapsed = max(0, min(total, (now-start).total_seconds()))
        filled = int((elapsed/total) * length)
        return "[" + "█" * filled + "░" * (length-filled) + "]"
    except Exception:
        return "[░░░░░░░░░░]"

# ========== SOS КОМАНДЫ ==========
def sos_activate(update, context):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        update.message.reply_text("Нет прав для активации SOS.")
        return
    if not SOS_PASSWORD:
        update.message.reply_text("SOS система не настроена (отсутствует пароль).")
        return
    args = context.args
    if len(args) != 1 or args[0] != SOS_PASSWORD:
        update.message.reply_text("Неверный пароль. Команда: /sdv <пароль>")
        return
    if is_sos_active():
        update.message.reply_text("SOS режим уже активен.")
        return
    # Активация
    activate_sos()
    # Баним всех пользователей (кроме админов)
    db.ban_all_users(except_admins=True)
    # Завершаем все активные розыгрыши
    finished = db.finish_all_active_giveaways()
    # Очищаем капча-хранилище
    captcha_storage.clear()
    # Отправляем уведомление в канал
    try:
        context.bot.send_message(chat_id=CHANNEL_ID, text="🔴 ВНИМАНИЕ! Аварийный режим активирован.\nВсе розыгрыши принудительно завершены, пользователи забанены.\nБот временно недоступен для обычных пользователей.")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение в канал: {e}")
    update.message.reply_text(f"✅ SOS режим активирован.\nЗабанены все пользователи, завершено {finished} розыгрышей.\nДля выхода используйте /sdv_off <пароль>")

def sos_deactivate(update, context):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        update.message.reply_text("Нет прав для деактивации SOS.")
        return
    if not SOS_PASSWORD:
        update.message.reply_text("SOS система не настроена.")
        return
    args = context.args
    if len(args) != 1 or args[0] != SOS_PASSWORD:
        update.message.reply_text("Неверный пароль. Команда: /sdv_off <пароль>")
        return
    if not is_sos_active():
        update.message.reply_text("SOS режим не активен.")
        return
    deactivate_sos()
    update.message.reply_text("✅ SOS режим деактивирован. Бот работает в обычном режиме.")

# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ (с проверкой SOS и rate limit) ==========
def start(update, context):
    user = update.effective_user
    if is_sos_active() and not is_admin(user.id):
        update.message.reply_text("🔴 Бот временно недоступен по техническим причинам.")
        return
    if not check_rate_limit(user.id):
        update.message.reply_text("Слишком много запросов. Подождите.")
        return
    db.add_user(user.id, user.username or "", user.first_name or "", user.last_name or "")
    db.update_user_activity(user.id)
    try:
        ip_seed = extract_ip_seed(update)
        db.add_ip_address(user.id, ip_seed)
    except Exception:
        pass
    if db.is_banned(user.id):
        update.message.reply_text("Вы забанены")
        return
    if context.args and context.args[0].startswith("ref_"):
        try:
            parts = context.args[0].split("_")
            if len(parts) == 3:
                giveaway_id = int(parts[1])
                referrer_id = int(parts[2])
                context.user_data["referrer"] = referrer_id
                context.user_data["giveaway"] = giveaway_id
                update.message.reply_text("Привет! Сначала пройдите проверку: /verify")
                return
        except Exception:
            pass
    keyboard = [
        [InlineKeyboardButton("Пройти проверку", callback_data="cmd_verify")],
        [InlineKeyboardButton("Мои рефералы", callback_data="cmd_my_referrals")],
        [InlineKeyboardButton("Топ рефереров", callback_data="cmd_top")],
        [InlineKeyboardButton("Помощь", callback_data="cmd_help")]
    ]
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("Админ-панель", callback_data="cmd_admin")])
    markup = InlineKeyboardMarkup(keyboard)
    text = f"Привет, {user.first_name}!\n\nБот для розыгрышей\n\nВыберите действие:"
    update.message.reply_text(text, reply_markup=markup)

def verify(update, context):
    if is_sos_active():
        if update.callback_query:
            update.callback_query.answer("Бот временно недоступен", show_alert=True)
        else:
            update.message.reply_text("🔴 Бот временно недоступен.")
        return
    if update.callback_query:
        message = update.callback_query.message
        user_id = update.effective_user.id
        update.callback_query.answer()
    else:
        message = update.message
        user_id = update.effective_user.id
    if not check_rate_limit(user_id):
        message.reply_text("Слишком много запросов.")
        return
    if message.chat.type != "private":
        message.reply_text("Только в личных сообщениях!")
        return
    if db.is_banned(user_id):
        message.reply_text("Вы забанены")
        return
    if db.is_verified(user_id):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data="cmd_start")]])
        message.reply_text("Вы уже верифицированы!", reply_markup=markup)
        return
    q, a = generate_captcha()
    ip_seed = extract_ip_seed(update)
    ip_hash = hashlib.sha256(ip_seed.encode()).hexdigest()[:32]
    captcha_storage[user_id] = {"answer": a, "attempts": 0, "time": datetime.now(), "ip_hash": ip_hash}
    message.reply_text(f"Пройдите проверку\n\nРешите: {q} = ?\n\nОтправьте ответ числом.")

def handle_text(update, context):
    user_id = update.effective_user.id
    if is_sos_active() and not is_admin(user_id):
        return
    if not check_rate_limit(user_id):
        update.message.reply_text("Слишком много запросов.")
        return
    if update.message.chat.type != "private":
        return
    text = update.message.text.strip()
    if db.is_banned(user_id):
        return
    if user_id in captcha_storage:
        captcha = captcha_storage[user_id]
        if datetime.now() - captcha["time"] > timedelta(minutes=5):
            update.message.reply_text("Время вышло. /verify")
            db.record_verification_attempt(user_id, success=False, ip_hash=captcha.get("ip_hash"))
            del captcha_storage[user_id]
            return
        if text == captcha["answer"]:
            if db.verify_user(user_id, method="captcha", ip_hash=captcha.get("ip_hash")):
                del captcha_storage[user_id]
                if db.check_multiple_accounts(user_id):
                    update.message.reply_text("Обнаружены мультиаккаунты.")
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("Главное меню", callback_data="cmd_start")]])
                update.message.reply_text("Проверка пройдена!\n\nТеперь можете участвовать!", reply_markup=markup)
            else:
                update.message.reply_text("Ошибка! Попробуйте /verify")
        else:
            captcha["attempts"] += 1
            db.record_verification_attempt(user_id, success=False, ip_hash=captcha.get("ip_hash"))
            if captcha["attempts"] >= 3:
                update.message.reply_text("Попытки закончились. /verify")
                del captcha_storage[user_id]
            else:
                update.message.reply_text(f"Неверно. Осталось: {3 - captcha['attempts']}")

def my_referrals(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id
    if is_sos_active() and not is_admin(user_id):
        if message: message.edit_text("Бот временно недоступен.")
        else: update.message.reply_text("Бот временно недоступен.")
        return
    if not check_rate_limit(user_id):
        update.message.reply_text("Слишком много запросов.")
        return
    if db.is_banned(user_id):
        if message: message.edit_text("Вы забанены")
        else: update.message.reply_text("Вы забанены")
        return
    if not db.is_verified(user_id):
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Пройти проверку", callback_data="cmd_verify")], [InlineKeyboardButton("Назад", callback_data="cmd_start")]])
        if message: message.edit_text("Сначала пройдите проверку", reply_markup=markup)
        else: update.message.reply_text("Сначала пройдите проверку: /verify", reply_markup=markup)
        return
    active = db.get_active_giveaways()
    if not active:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]])
        if message: message.edit_text("Нет активных розыгрышей", reply_markup=markup)
        else: update.message.reply_text("Нет активных розыгрышей", reply_markup=markup)
        return
    text = "Ваши реферальные ссылки:\n\n"
    bot_username = context.bot.get_me().username
    for g in active:
        gid, name, winners, end_date, require_sub = g
        ref_count = db.get_referral_count(user_id, gid)
        bonus = db.get_bonus_entries(user_id, gid)
        link = f"https://t.me/{bot_username}?start=ref_{gid}_{user_id}"
        time_left = format_time_left(end_date)
        text += f"{name}\n{link}\nПриглашено: {ref_count}\nБонусов: {bonus}\nОсталось: {time_left}\n------\n"
    text += "\nОтправьте ссылку друзьям!"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]])
    if message: message.edit_text(text, reply_markup=markup)
    else: update.message.reply_text(text, reply_markup=markup)

def top_referrers(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id
    if is_sos_active() and not is_admin(user_id):
        if message: message.edit_text("Бот временно недоступен.")
        else: update.message.reply_text("Бот временно недоступен.")
        return
    if not check_rate_limit(user_id):
        update.message.reply_text("Слишком много запросов.")
        return
    top = db.get_top_referrers(10)
    if not top:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]])
        if message: message.edit_text("Пока нет рефереров", reply_markup=markup)
        else: update.message.reply_text("Пока нет рефереров", reply_markup=markup)
        return
    text = "Топ-10 рефереров:\n\n"
    medals = ["🥇","🥈","🥉"]
    for i, (uid, uname, fname, cnt) in enumerate(top, 1):
        medal = medals[i-1] if i<=3 else f"{i}."
        name_str = f"@{uname}" if uname else fname
        text += f"{medal} {name_str} - {cnt}\n"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Мои рефералы", callback_data="cmd_my_referrals")], [InlineKeyboardButton("Назад", callback_data="cmd_start")]])
    if message: message.edit_text(text, reply_markup=markup)
    else: update.message.reply_text(text, reply_markup=markup)

def help_cmd(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id
    if is_sos_active() and not is_admin(user_id):
        if message: message.edit_text("Бот временно недоступен.")
        else: update.message.reply_text("Бот временно недоступен.")
        return
    if not check_rate_limit(user_id):
        update.message.reply_text("Слишком много запросов.")
        return
    text = "Помощь\n\nПользователь:\n/start - Начать\n/verify - Проверка\n/my_referrals - Рефералы\n/top - Топ рефереров\n/help - Помощь\n"
    if is_admin(user_id):
        text += "\nАдмин:\n/new <название> <победителей> [часы] [описание] [sub]\n/list - Список\n/end <id> - Завершить\n/stats <id> - Статистика\n/participants <id> - Участники\n/remove <gid> <uid> - Удалить участника\n/ban <uid> [причина] [дней]\n/unban <uid>\n/banned - Список забаненных\n/check_multi [порог]\n/verify_info <uid>\n\n🔒 SOS:\n/sdv <пароль> - Активировать аварийный режим\n/sdv_off <пароль> - Деактивировать"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]])
    if message: message.edit_text(text, reply_markup=markup)
    else: update.message.reply_text(text, reply_markup=markup)

def admin_panel(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id
    if is_sos_active() and not is_admin(user_id):
        if message: message.edit_text("Бот временно недоступен.")
        else: update.message.reply_text("Бот временно недоступен.")
        return
    if not is_admin(user_id):
        if message: message.edit_text("Нет прав", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]]))
        else: update.message.reply_text("Нет прав")
        return
    text = "Админ-панель\n\nВыберите действие:"
    keyboard = [
        [InlineKeyboardButton("Создать розыгрыш", callback_data="admin_new")],
        [InlineKeyboardButton("Список розыгрышей", callback_data="admin_list")],
        [InlineKeyboardButton("Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("Назад", callback_data="cmd_start")]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if message: message.edit_text(text, reply_markup=markup)
    else: update.message.reply_text(text, reply_markup=markup)

def new_giveaway(update, context):
    if is_sos_active() and not is_admin(update.effective_user.id):
        update.message.reply_text("Бот временно недоступен.")
        return
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if len(context.args) < 2:
        update.message.reply_text("Использование: /new <название> <победителей> [часы] [описание]\nДобавьте 'sub' в конце для проверки подписки")
        return
    try:
        name = context.args[0]
        winners = int(context.args[1])
        hours = int(context.args[2]) if len(context.args) > 2 and context.args[2].isdigit() else 24
        require_sub = 0
        desc_parts = []
        start_index = 3 if len(context.args) > 2 and context.args[2].isdigit() else 2
        for i in range(start_index, len(context.args)):
            if context.args[i].lower() == "sub":
                require_sub = 1
            else:
                desc_parts.append(context.args[i])
        description = " ".join(desc_parts) if desc_parts else "Розыгрыш"
        giveaway_id = db.create_giveaway(name, description, winners, hours, str(CHANNEL_ID), require_sub)
        if not giveaway_id:
            update.message.reply_text("Ошибка создания")
            return
        info = db.get_giveaway_info(giveaway_id)
        start_date = info[4]
        end_date = info[5]
        progress = create_progress_bar(start_date, end_date)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Участвовать", callback_data=f"join_{giveaway_id}")]])
        sub_text = "\n\nТребуется подписка на канал!" if require_sub else ""
        msg = context.bot.send_message(chat_id=CHANNEL_ID, text=f"НОВЫЙ РОЗЫГРЫШ!\n\n{name}\n{description}\n\nПобедителей: {winners}\nЗавершится: {datetime.fromisoformat(end_date).strftime('%d.%m.%Y в %H:%M')}\n\n{progress}\n{format_time_left(end_date)}{sub_text}\n\nНажмите кнопку!", reply_markup=markup)
        db.update_message_id(giveaway_id, msg.message_id)
        update.message.reply_text(f"Розыгрыш создан! ID: {giveaway_id}" + ("\nПроверка подписки: ВКЛ" if require_sub else ""))
    except Exception as e:
        update.message.reply_text(f"Ошибка: {e}")

def list_giveaways_cmd(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id
    if is_sos_active() and not is_admin(user_id):
        if message: message.edit_text("Бот временно недоступен.")
        else: update.message.reply_text("Бот временно недоступен.")
        return
    if not is_admin(user_id):
        if message: message.edit_text("Нет прав")
        else: update.message.reply_text("Нет прав")
        return
    giveaways = db.get_active_giveaways()
    if not giveaways:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_admin")]])
        if message: message.edit_text("Нет розыгрышей", reply_markup=markup)
        else: update.message.reply_text("Нет розыгрышей", reply_markup=markup)
        return
    text = "Активные розыгрыши:\n\n"
    for g in giveaways:
        gid, name, winners, end_date, req = g
        info = db.get_giveaway_info(gid)
        start_date = info[4] if info else datetime.now().isoformat()
        participants = db.get_participants_count(gid)
        progress = create_progress_bar(start_date, end_date)
        text += f"ID: {gid}\n{name}\nУчастников: {participants}\n{progress} {format_time_left(end_date)}\n------\n"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_admin")]])
    if message: message.edit_text(text, reply_markup=markup)
    else: update.message.reply_text(text, reply_markup=markup)

def finish_giveaway(bot, giveaway_id, message=None):
    try:
        info = db.get_giveaway_info(giveaway_id)
        if not info:
            if message: message.reply_text("Не найден")
            return
        participants = db.get_participants(giveaway_id)
        if not participants:
            db.end_giveaway(giveaway_id)
            if message: message.reply_text("Нет участников")
            return
        winner_count = min(info[3], len(participants))
        winners = random.sample(participants, winner_count)
        winners_text = "ПОБЕДИТЕЛИ!\n\n"
        for i, wid in enumerate(winners, 1):
            try:
                user = bot.get_chat(wid)
                username = f"@{user.username}" if user.username else user.first_name
                winners_text += f"{i}. {username}\n"
            except:
                winners_text += f"{i}. ID: {wid}\n"
        db.end_giveaway(giveaway_id)
        try:
            bot.send_message(chat_id=CHANNEL_ID, text=winners_text)
        except:
            pass
        if message: message.reply_text("Завершен!\n\n" + winners_text)
        logger.info(f"Giveaway {giveaway_id} finished")
    except Exception as e:
        logger.error(f"finish_giveaway error: {e}")

def end_giveaway(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if not context.args:
        update.message.reply_text("Использование: /end <id>")
        return
    try:
        gid = int(context.args[0])
        finish_giveaway(context.bot, gid, update.message)
    except Exception as e:
        update.message.reply_text(f"Ошибка: {e}")

def auto_finish_thread(bot):
    while True:
        try:
            for gid in db.get_giveaways_to_finish():
                finish_giveaway(bot, gid)
            time.sleep(60)
        except Exception as e:
            logger.error(f"Auto-finish error: {e}")
            time.sleep(60)

def stats(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if not context.args:
        update.message.reply_text("Использование: /stats <id>")
        return
    try:
        gid = int(context.args[0])
        info = db.get_giveaway_info(gid)
        if not info:
            update.message.reply_text("Не найден")
            return
        _, name, desc, winners, start, end, active, _, _, _, req = info
        participants = db.get_participants_count(gid)
        status = "Активен" if active else "Завершен"
        progress = create_progress_bar(start, end)
        text = f"Статистика #{gid}\n\n{name}\n{desc}\nПобедителей: {winners}\nУчастников: {participants}\nСтатус: {status}\nПроверка подписки: {'ВКЛ' if req else 'ВЫКЛ'}\n\n{progress} {format_time_left(end)}\n\n{datetime.fromisoformat(start).strftime('%d.%m %H:%M')} - {datetime.fromisoformat(end).strftime('%d.%m %H:%M')}"
        update.message.reply_text(text)
    except Exception as e:
        update.message.reply_text(f"Ошибка: {e}")

def participants_cmd(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if not context.args:
        update.message.reply_text("Использование: /participants <id>")
        return
    try:
        gid = int(context.args[0])
        parts = db.get_participants_with_info(gid)
        if not parts:
            update.message.reply_text("Нет участников")
            return
        info = db.get_giveaway_info(gid)
        name = info[1] if info else f"#{gid}"
        text = f"Участники '{name}'\nВсего: {len(parts)}\n\n"
        for i, (uid, uname, fname, banned, join) in enumerate(parts[:50], 1):
            status = "BAN" if banned else "OK"
            uname_str = f"@{uname}" if uname else "нет"
            text += f"{i}. {status} {fname} ({uname_str}) - {uid}\n"
        if len(parts) > 50:
            text += f"\n...и еще {len(parts)-50}"
        update.message.reply_text(text)
    except Exception as e:
        update.message.reply_text(f"Ошибка: {e}")

def remove_participant(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if len(context.args) < 2:
        update.message.reply_text("Использование: /remove <giveaway_id> <user_id>")
        return
    try:
        gid, uid = int(context.args[0]), int(context.args[1])
        if db.remove_participant(gid, uid):
            update.message.reply_text(f"Участник {uid} удален из {gid}")
        else:
            update.message.reply_text("Не найден")
    except Exception as e:
        update.message.reply_text(f"Ошибка: {e}")

def ban_user(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if len(context.args) < 2:
        update.message.reply_text("Использование: /ban <user_id> <причина> [дней]")
        return
    try:
        uid = int(context.args[0])
        if len(context.args) > 2 and context.args[-1].isdigit():
            days = int(context.args[-1])
            reason = " ".join(context.args[1:-1])
        else:
            days = 30
            reason = " ".join(context.args[1:])
        admin_id = update.effective_user.id
        if db.ban_user(uid, admin_id, reason, days):
            try:
                context.bot.send_message(chat_id=uid, text=f"ВЫ ЗАБАНЕНЫ!\n\nПричина: {reason}\nСрок: {days} дней")
            except:
                pass
            update.message.reply_text(f"Пользователь {uid} забанен\nПричина: {reason}\nСрок: {days} дней")
        else:
            update.message.reply_text("Ошибка")
    except Exception as e:
        update.message.reply_text(f"Ошибка: {e}")

def unban_user(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if not context.args:
        update.message.reply_text("Использование: /unban <user_id>")
        return
    try:
        uid = int(context.args[0])
        if db.unban_user(uid):
            try:
                context.bot.send_message(chat_id=uid, text="Вы разбанены!")
            except:
                pass
            update.message.reply_text(f"Пользователь {uid} разбанен")
        else:
            update.message.reply_text("Ошибка")
    except Exception as e:
        update.message.reply_text(f"Ошибка: {e}")

def banned_list(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    banned = db.get_banned_users()
    if not banned:
        update.message.reply_text("Нет забаненных")
        return
    text = f"Забаненные ({len(banned)})\n\n"
    for user in banned[:30]:
        uid, uname, fname, reason, date = user
        date_str = datetime.fromisoformat(date).strftime("%d.%m.%Y") if date else "неизвестно"
        uname_str = f"@{uname}" if uname else "нет"
        text += f"{fname} ({uname_str}) - {uid}\n{reason}\nДата: {date_str}\n------\n"
    if len(banned) > 30:
        text += f"\n...и еще {len(banned)-30}"
    update.message.reply_text(text)

def check_multi(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    threshold = int(context.args[0]) if context.args and context.args[0].isdigit() else 2
    ips = db.get_suspicious_ips(threshold)
    if not ips:
        update.message.reply_text("Подозрительных IP не найдено")
        return
    text = f"Подозрительные IP (порог {threshold})\n\n"
    for ip, count, last in ips[:30]:
        users = db.get_users_by_ip(ip)
        text += f"IP: {ip}\nПользователей: {count}\nПоследний раз: {last}\n"
        for uid, uname, fname, joined in users[:5]:
            uname_str = f"@{uname}" if uname else "нет"
            text += f"- {fname} ({uname_str}) {uid}\n"
        if len(users) > 5:
            text += f"...и еще {len(users)-5}\n"
        text += "------\n"
    update.message.reply_text(text)

def verify_info(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if not context.args:
        update.message.reply_text("Использование: /verify_info <user_id>")
        return
    try:
        uid = int(context.args[0])
        info = db.get_verification_info(uid)
        if not info:
            update.message.reply_text("Пользователь не найден")
            return
        verified, date, method, attempts = info
        text = f"Информация о проверке #{uid}\n\nВерифицирован: {'да' if verified else 'нет'}\nДата: {date or 'нет'}\nМетод: {method or 'нет'}\nПопыток: {attempts}"
        update.message.reply_text(text)
    except Exception as e:
        update.message.reply_text(f"Ошибка: {e}")

def button_handler(update, context):
    query = update.callback_query
    query.answer()
    data = query.data
    if is_sos_active() and not is_admin(update.effective_user.id):
        query.answer("Бот временно недоступен", show_alert=True)
        return
    if data == "cmd_start":
        user = update.effective_user
        keyboard = [
            [InlineKeyboardButton("Пройти проверку", callback_data="cmd_verify")],
            [InlineKeyboardButton("Мои рефералы", callback_data="cmd_my_referrals")],
            [InlineKeyboardButton("Топ рефереров", callback_data="cmd_top")],
            [InlineKeyboardButton("Помощь", callback_data="cmd_help")]
        ]
        if is_admin(user.id):
            keyboard.append([InlineKeyboardButton("Админ-панель", callback_data="cmd_admin")])
        markup = InlineKeyboardMarkup(keyboard)
        text = f"Привет, {user.first_name}!\n\nБот для розыгрышей\n\nВыберите действие:"
        query.edit_message_text(text, reply_markup=markup)
    elif data == "cmd_verify":
        verify(update, context)
    elif data == "cmd_my_referrals":
        my_referrals(update, context, message=query.message)
    elif data == "cmd_top":
        top_referrers(update, context, message=query.message)
    elif data == "cmd_help":
        help_cmd(update, context, message=query.message)
    elif data == "cmd_admin":
        admin_panel(update, context, message=query.message)
    elif data == "admin_new":
        query.edit_message_text("Создание розыгрыша через команду:\n/new <название> <победителей> [часы] [описание]\nДобавьте 'sub' в конце для проверки подписки")
    elif data == "admin_list":
        list_giveaways_cmd(update, context, message=query.message)
    elif data == "admin_stats":
        query.edit_message_text("Используйте команду: /stats <id>")
    elif data.startswith("join_"):
        gid = int(data.split("_")[1])
        uid = update.effective_user.id
        if db.is_banned(uid):
            query.answer("❌ Вы забанены", show_alert=True)
            return
        if not db.is_verified(uid):
            query.answer("🔐 Требуется верификация", show_alert=True)
            context.bot.send_message(chat_id=uid, text="Для участия в розыгрышах нужно пройти проверку.\nИспользуйте команду /verify.")
            return
        info = db.get_giveaway_info(gid)
        if not info or info[6] != 1:
            query.answer("❌ Розыгрыш не найден или завершён", show_alert=True)
            return
        require_sub = info[10] if len(info) > 10 else 0
        if require_sub and not check_subscription(context.bot, uid, CHANNEL_ID):
            query.answer("📢 Подпишитесь на канал, чтобы участвовать", show_alert=True)
            return
        if db.add_participant(gid, uid, context.user_data.get("referrer")):
            query.answer("✅ Вы успешно участвуете!", show_alert=True)
        else:
            query.answer("ℹ️ Вы уже участвуете", show_alert=True)

def error_handler(update, context):
    logger.error(f"Error: {context.error}")

def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("verify", verify))
    dp.add_handler(CommandHandler("my_referrals", my_referrals))
    dp.add_handler(CommandHandler("top", top_referrers))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("new", new_giveaway))
    dp.add_handler(CommandHandler("list", list_giveaways_cmd))
    dp.add_handler(CommandHandler("end", end_giveaway))
    dp.add_handler(CommandHandler("stats", stats))
    dp.add_handler(CommandHandler("participants", participants_cmd))
    dp.add_handler(CommandHandler("remove", remove_participant))
    dp.add_handler(CommandHandler("ban", ban_user))
    dp.add_handler(CommandHandler("unban", unban_user))
    dp.add_handler(CommandHandler("banned", banned_list))
    dp.add_handler(CommandHandler("check_multi", check_multi))
    dp.add_handler(CommandHandler("verify_info", verify_info))
    dp.add_handler(CommandHandler("sdv", sos_activate))
    dp.add_handler(CommandHandler("sdv_off", sos_deactivate))
    dp.add_handler(CallbackQueryHandler(button_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
    dp.add_error_handler(error_handler)
    threading.Thread(target=auto_finish_thread, args=(updater.bot,), daemon=True).start()
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
