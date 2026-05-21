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

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = [5207853162, 5406117718]
CHANNEL_ID = -1002376241083

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_name=None):
        if db_name is None:
            db_name = os.getenv("DB_PATH", "giveaway.db")
        # Создаём директорию для файла БД, если её нет
        db_dir = os.path.dirname(db_name)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()

    def _execute(self, query, params=(), fetchone=False, fetchall=False, commit=False):
        with self.lock:
            self.cursor.execute(query, params)
            result = None
            if fetchone:
                result = self.cursor.fetchone()
            elif fetchall:
                result = self.cursor.fetchall()
            if commit:
                self.conn.commit()
            return result

    def create_tables(self):
        with self.lock:
            self.cursor.execute("""
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
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS verification_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    verification_type TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    attempt_date TEXT NOT NULL,
                    ip_hash TEXT
                )
            """)
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS ban_list (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    admin_id INTEGER,
                    reason TEXT,
                    ban_date TEXT NOT NULL,
                    unban_date TEXT
                )
            """)
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS ip_addresses (
                    ip_hash TEXT PRIMARY KEY,
                    user_count INTEGER DEFAULT 1,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                )
            """)
            self.cursor.execute("""
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
            self.cursor.execute("""
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
            self.cursor.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_id INTEGER NOT NULL,
                    referred_id INTEGER NOT NULL,
                    giveaway_id INTEGER NOT NULL,
                    referral_date TEXT NOT NULL,
                    UNIQUE(referrer_id, referred_id, giveaway_id)
                )
            """)
            self.conn.commit()

    def add_user(self, user_id, username, first_name, last_name=""):
        try:
            current_time = datetime.now().isoformat()
            exists = self._execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (user_id,),
                fetchone=True
            )
            with self.lock:
                if exists:
                    self.cursor.execute("""
                        UPDATE users
                        SET username = ?, first_name = ?, last_name = ?, last_activity = ?
                        WHERE user_id = ?
                    """, (username, first_name, last_name, current_time, user_id))
                else:
                    self.cursor.execute("""
                        INSERT INTO users (
                            user_id, username, first_name, last_name,
                            joined_date, last_activity, is_verified
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 0)
                    """, (user_id, username, first_name, last_name, current_time, current_time))
                self.conn.commit()
            return True
        except Exception as e:
            print(f"add_user error: {e}")
            return False

    def verify_user(self, user_id, method="captcha", ip_hash=None):
        try:
            current_time = datetime.now().isoformat()
            with self.lock:
                self.cursor.execute("""
                    UPDATE users
                    SET is_verified = 1,
                        verification_date = ?,
                        verification_method = ?,
                        verification_attempts = verification_attempts + 1
                    WHERE user_id = ?
                """, (current_time, method, user_id))
                self.cursor.execute("""
                    INSERT INTO verification_history (
                        user_id, verification_type, success, attempt_date, ip_hash
                    )
                    VALUES (?, ?, 1, ?, ?)
                """, (user_id, method, current_time, ip_hash))
                self.conn.commit()
            return True
        except Exception as e:
            print(f"verify_user error: {e}")
            self.conn.rollback()
            return False

    def is_verified(self, user_id):
        try:
            result = self._execute(
                "SELECT is_verified FROM users WHERE user_id = ?",
                (user_id,),
                fetchone=True
            )
            return bool(result and result[0] == 1)
        except Exception as e:
            print(f"is_verified error: {e}")
            return False

    def record_verification_attempt(self, user_id, success=False, method="captcha", ip_hash=None):
        try:
            current_time = datetime.now().isoformat()
            with self.lock:
                self.cursor.execute("""
                    UPDATE users
                    SET verification_attempts = verification_attempts + 1
                    WHERE user_id = ?
                """, (user_id,))
                self.cursor.execute("""
                    INSERT INTO verification_history (
                        user_id, verification_type, success, attempt_date, ip_hash
                    )
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, method, 1 if success else 0, current_time, ip_hash))
                self.conn.commit()
            return True
        except Exception:
            return False

    def update_user_activity(self, user_id):
        try:
            self._execute(
                "UPDATE users SET last_activity = ? WHERE user_id = ?",
                (datetime.now().isoformat(), user_id),
                commit=True
            )
        except Exception:
            pass

    def ban_user(self, user_id, admin_id, reason="Нарушение", days=30):
        try:
            current_time = datetime.now()
            unban_date = current_time + timedelta(days=days)
            with self.lock:
                self.cursor.execute("""
                    INSERT INTO ban_list (user_id, admin_id, reason, ban_date, unban_date)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, admin_id, reason, current_time.isoformat(), unban_date.isoformat()))
                self.cursor.execute("""
                    UPDATE users
                    SET is_banned = 1, ban_reason = ?, banned_date = ?
                    WHERE user_id = ?
                """, (reason, current_time.isoformat(), user_id))
                self.cursor.execute("""
                    UPDATE participants
                    SET is_valid = 0
                    WHERE user_id = ?
                """, (user_id,))
                self.conn.commit()
            return True
        except Exception:
            return False

    def unban_user(self, user_id):
        try:
            self._execute("""
                UPDATE users
                SET is_banned = 0, ban_reason = NULL, banned_date = NULL
                WHERE user_id = ?
            """, (user_id,), commit=True)
            return True
        except Exception:
            return False

    def is_banned(self, user_id):
        try:
            result = self._execute(
                "SELECT is_banned FROM users WHERE user_id = ?",
                (user_id,),
                fetchone=True
            )
            return bool(result and result[0] == 1)
        except Exception:
            return False

    def get_ban_info(self, user_id):
        try:
            return self._execute(
                "SELECT ban_reason, banned_date FROM users WHERE user_id = ? AND is_banned = 1",
                (user_id,),
                fetchone=True
            )
        except Exception:
            return None

    def get_banned_users(self):
        try:
            return self._execute(
                "SELECT user_id, username, first_name, ban_reason, banned_date FROM users WHERE is_banned = 1 ORDER BY banned_date DESC",
                fetchall=True
            ) or []
        except Exception:
            return []

    def add_ip_address(self, user_id, ip_seed):
        try:
            ip_hash = hashlib.sha256(ip_seed.encode()).hexdigest()[:32]
            current_time = datetime.now().isoformat()
            with self.lock:
                self.cursor.execute("UPDATE users SET ip_hash = ? WHERE user_id = ?", (ip_hash, user_id))
                self.cursor.execute("SELECT user_count FROM ip_addresses WHERE ip_hash = ?", (ip_hash,))
                exists = self.cursor.fetchone()
                if exists:
                    self.cursor.execute("""
                        UPDATE ip_addresses
                        SET user_count = user_count + 1, last_seen = ?
                        WHERE ip_hash = ?
                    """, (current_time, ip_hash))
                else:
                    self.cursor.execute("""
                        INSERT INTO ip_addresses (ip_hash, user_count, first_seen, last_seen)
                        VALUES (?, 1, ?, ?)
                    """, (ip_hash, current_time, current_time))
                self.conn.commit()
            return ip_hash
        except Exception:
            return None

    def get_suspicious_ips(self, threshold=2):
        try:
            return self._execute(
                "SELECT ip_hash, user_count, last_seen FROM ip_addresses WHERE user_count >= ? ORDER BY user_count DESC",
                (threshold,),
                fetchall=True
            ) or []
        except Exception:
            return []

    def get_users_by_ip(self, ip_hash):
        try:
            return self._execute(
                "SELECT user_id, username, first_name, joined_date FROM users WHERE ip_hash = ? ORDER BY joined_date",
                (ip_hash,),
                fetchall=True
            ) or []
        except Exception:
            return []

    def check_multiple_accounts(self, user_id):
        try:
            result = self._execute(
                "SELECT ip_hash FROM users WHERE user_id = ?",
                (user_id,),
                fetchone=True
            )
            if not result or not result[0]:
                return []
            ip_hash = result[0]
            rows = self._execute(
                "SELECT user_id FROM users WHERE ip_hash = ? AND user_id != ?",
                (ip_hash, user_id),
                fetchall=True
            ) or []
            return [row[0] for row in rows]
        except Exception:
            return []

    def create_giveaway(self, name, description, winners, hours, channel_id, require_sub=0):
        try:
            start_date = datetime.now()
            end_date = start_date + timedelta(hours=hours)
            with self.lock:
                self.cursor.execute("""
                    INSERT INTO giveaways (
                        name, description, winner_count, start_date, end_date,
                        is_active, channel_id, auto_finish, require_subscription
                    )
                    VALUES (?, ?, ?, ?, ?, 1, ?, 1, ?)
                """, (name, description, winners, start_date.isoformat(), end_date.isoformat(), channel_id, require_sub))
                self.conn.commit()
                return self.cursor.lastrowid
        except Exception:
            return None

    def update_message_id(self, giveaway_id, message_id):
        try:
            self._execute(
                "UPDATE giveaways SET message_id = ? WHERE id = ?",
                (message_id, giveaway_id),
                commit=True
            )
        except Exception:
            pass

    def add_participant(self, giveaway_id, user_id, referred_by=None):
        try:
            current_time = datetime.now().isoformat()
            with self.lock:
                self.cursor.execute("""
                    INSERT INTO participants (giveaway_id, user_id, join_date, referred_by)
                    VALUES (?, ?, ?, ?)
                """, (giveaway_id, user_id, current_time, referred_by))
                if referred_by:
                    try:
                        self.cursor.execute("""
                            INSERT INTO referrals (referrer_id, referred_id, giveaway_id, referral_date)
                            VALUES (?, ?, ?, ?)
                        """, (referred_by, user_id, giveaway_id, current_time))
                        self.cursor.execute("""
                            UPDATE participants
                            SET bonus_entries = bonus_entries + 1
                            WHERE giveaway_id = ? AND user_id = ?
                        """, (giveaway_id, referred_by))
                    except Exception:
                        pass
                self.conn.commit()
            return True
        except Exception:
            return False

    def get_referral_count(self, user_id, giveaway_id):
        try:
            result = self._execute("""
                SELECT COUNT(*)
                FROM referrals
                WHERE referrer_id = ? AND giveaway_id = ?
            """, (user_id, giveaway_id), fetchone=True)
            return result[0] if result else 0
        except Exception:
            return 0

    def get_bonus_entries(self, user_id, giveaway_id):
        try:
            result = self._execute("""
                SELECT bonus_entries
                FROM participants
                WHERE giveaway_id = ? AND user_id = ?
            """, (giveaway_id, user_id), fetchone=True)
            return result[0] if result else 0
        except Exception:
            return 0

    def get_top_referrers(self, limit=10):
        try:
            return self._execute("""
                SELECT r.referrer_id, u.username, u.first_name, COUNT(r.referred_id) AS ref_count
                FROM referrals r
                LEFT JOIN users u ON r.referrer_id = u.user_id
                GROUP BY r.referrer_id
                ORDER BY ref_count DESC
                LIMIT ?
            """, (limit,), fetchall=True) or []
        except Exception:
            return []

    def remove_participant(self, giveaway_id, user_id):
        try:
            self._execute("""
                UPDATE participants
                SET is_valid = 0
                WHERE giveaway_id = ? AND user_id = ?
            """, (giveaway_id, user_id), commit=True)
            return True
        except Exception:
            return False

    def get_active_giveaways(self):
        try:
            return self._execute("""
                SELECT id, name, winner_count, end_date, require_subscription
                FROM giveaways
                WHERE is_active = 1
                ORDER BY end_date
            """, fetchall=True) or []
        except Exception:
            return []

    def get_giveaway_info(self, giveaway_id):
        try:
            return self._execute("""
                SELECT id, name, description, winner_count, start_date, end_date,
                       is_active, message_id, channel_id, auto_finish, require_subscription
                FROM giveaways
                WHERE id = ?
            """, (giveaway_id,), fetchone=True)
        except Exception:
            return None

    def get_participants(self, giveaway_id, valid_only=True):
        try:
            if valid_only:
                rows = self._execute("""
                    SELECT user_id
                    FROM participants
                    WHERE giveaway_id = ? AND is_valid = 1
                """, (giveaway_id,), fetchall=True) or []
            else:
                rows = self._execute("""
                    SELECT user_id
                    FROM participants
                    WHERE giveaway_id = ?
                """, (giveaway_id,), fetchall=True) or []
            return [row[0] for row in rows]
        except Exception:
            return []

    def get_participants_with_info(self, giveaway_id):
        try:
            return self._execute("""
                SELECT p.user_id, u.username, u.first_name, u.is_banned, p.join_date
                FROM participants p
                LEFT JOIN users u ON p.user_id = u.user_id
                WHERE p.giveaway_id = ? AND p.is_valid = 1
                ORDER BY p.join_date
            """, (giveaway_id,), fetchall=True) or []
        except Exception:
            return []

    def get_participants_count(self, giveaway_id):
        try:
            result = self._execute("""
                SELECT COUNT(*)
                FROM participants
                WHERE giveaway_id = ? AND is_valid = 1
            """, (giveaway_id,), fetchone=True)
            return result[0] if result else 0
        except Exception:
            return 0

    def end_giveaway(self, giveaway_id):
        try:
            self._execute(
                "UPDATE giveaways SET is_active = 0 WHERE id = ?",
                (giveaway_id,),
                commit=True
            )
            return True
        except Exception:
            return False

    def get_giveaways_to_finish(self):
        try:
            current_time = datetime.now().isoformat()
            rows = self._execute("""
                SELECT id
                FROM giveaways
                WHERE is_active = 1 AND auto_finish = 1 AND end_date <= ?
            """, (current_time,), fetchall=True) or []
            return [row[0] for row in rows]
        except Exception:
            return []

    def get_verification_info(self, user_id):
        try:
            result = self._execute(
                "SELECT is_verified, verification_date, verification_method, verification_attempts FROM users WHERE user_id = ?",
                (user_id,),
                fetchone=True
            )
            return result
        except Exception:
            return None


db = Database()
captcha_storage = {}


def generate_captcha():
    a = random.randint(1, 10)
    b = random.randint(1, 10)
    operation = random.choice(["+", "-", "*"])
    if operation == "+":
        return f"{a} + {b}", str(a + b)
    if operation == "-":
        return f"{a} - {b}", str(a - b)
    return f"{a} x {b}", str(a * b)


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
        end = datetime.fromisoformat(end_date)
        diff = end - datetime.now()
        if diff.total_seconds() <= 0:
            return "Завершен"
        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60
        if days > 0:
            return f"{days}д {hours}ч"
        if hours > 0:
            return f"{hours}ч {minutes}мин"
        return f"{minutes}мин"
    except Exception:
        return "Неизвестно"


def create_progress_bar(start_date, end_date, length=10):
    try:
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
        now = datetime.now()
        total = (end - start).total_seconds()
        if total <= 0:
            return "[" + "█" * length + "]"
        elapsed = max(0, min(total, (now - start).total_seconds()))
        progress = elapsed / total
        filled = int(progress * length)
        return "[" + "█" * filled + "░" * (length - filled) + "]"
    except Exception:
        return "[░░░░░░░░░░]"


def start(update, context):
    user = update.effective_user
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
    if update.callback_query:
        message = update.callback_query.message
        user_id = update.effective_user.id
        update.callback_query.answer()
    else:
        message = update.message
        user_id = update.effective_user.id

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

    question, answer = generate_captcha()
    ip_seed = extract_ip_seed(update)
    ip_hash = hashlib.sha256(ip_seed.encode()).hexdigest()[:32]
    captcha_storage[user_id] = {
        "answer": answer,
        "attempts": 0,
        "time": datetime.now(),
        "ip_hash": ip_hash
    }
    message.reply_text(
        f"Пройдите проверку\n\nРешите: {question} = ?\n\nОтправьте ответ числом."
    )


def handle_text(update, context):
    if update.message.chat.type != "private":
        return

    user_id = update.effective_user.id
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
            success = db.verify_user(user_id, method="captcha", ip_hash=captcha.get("ip_hash"))
            if success:
                del captcha_storage[user_id]
                multi_accounts = db.check_multiple_accounts(user_id)
                if multi_accounts:
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
                left = 3 - captcha["attempts"]
                update.message.reply_text(f"Неверно. Осталось: {left}")


def my_referrals(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id

    if db.is_banned(user_id):
        if message:
            message.edit_text("Вы забанены")
        else:
            update.message.reply_text("Вы забанены")
        return

    if not db.is_verified(user_id):
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Пройти проверку", callback_data="cmd_verify")],
            [InlineKeyboardButton("Назад", callback_data="cmd_start")]
        ])
        if message:
            message.edit_text("Сначала пройдите проверку", reply_markup=markup)
        else:
            update.message.reply_text("Сначала пройдите проверку: /verify", reply_markup=markup)
        return

    active_giveaways = db.get_active_giveaways()
    if not active_giveaways:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]])
        if message:
            message.edit_text("Нет активных розыгрышей", reply_markup=markup)
        else:
            update.message.reply_text("Нет активных розыгрышей", reply_markup=markup)
        return

    text = "Ваши реферальные ссылки:\n\n"
    bot_username = context.bot.get_me().username
    for g in active_giveaways:
        gid, name, winners, end_date, require_sub = g
        referral_count = db.get_referral_count(user_id, gid)
        bonus_entries = db.get_bonus_entries(user_id, gid)
        ref_link = f"https://t.me/{bot_username}?start=ref_{gid}_{user_id}"
        time_left = format_time_left(end_date)
        text += (
            f"{name}\n"
            f"{ref_link}\n"
            f"Приглашено: {referral_count}\n"
            f"Бонусов: {bonus_entries}\n"
            f"Осталось: {time_left}\n"
            "------\n"
        )
    text += "\nОтправьте ссылку друзьям!"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]])

    if message:
        message.edit_text(text, reply_markup=markup)
    else:
        update.message.reply_text(text, reply_markup=markup)


def top_referrers(update, context, message=None):
    top = db.get_top_referrers(10)
    if not top:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]])
        text = "Пока нет рефереров"
        if message:
            message.edit_text(text, reply_markup=markup)
        else:
            update.message.reply_text(text, reply_markup=markup)
        return

    text = "Топ-10 рефереров:\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (user_id, username, first_name, ref_count) in enumerate(top, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        username_str = f"@{username}" if username else first_name
        text += f"{medal} {username_str} - {ref_count}\n"

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Мои рефералы", callback_data="cmd_my_referrals")],
        [InlineKeyboardButton("Назад", callback_data="cmd_start")]
    ])

    if message:
        message.edit_text(text, reply_markup=markup)
    else:
        update.message.reply_text(text, reply_markup=markup)


def help_cmd(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id
    text = (
        "Помощь\n\n"
        "Пользователь:\n"
        "/start - Начать\n"
        "/verify - Проверка\n"
        "/my_referrals - Рефералы\n"
        "/top - Топ рефереров\n"
        "/help - Помощь\n"
    )
    if is_admin(user_id):
        text += (
            "\nАдмин:\n"
            "/new - Создать\n"
            "/list - Список\n"
            "/end - Завершить\n"
            "/stats - Статистика\n"
            "/participants - Участники\n"
            "/remove - Удалить\n"
            "/ban - Забанить\n"
            "/unban - Разбанить\n"
            "/banned - Забаненные\n"
            "/check_multi - Мультиаккаунты\n"
            "/verify_info - Инфо\n"
        )

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]])
    if message:
        message.edit_text(text, reply_markup=markup)
    else:
        update.message.reply_text(text, reply_markup=markup)


def admin_panel(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id
    if not is_admin(user_id):
        if message:
            message.edit_text("Нет прав", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_start")]]))
        else:
            update.message.reply_text("Нет прав")
        return

    text = "Админ-панель\n\nВыберите действие:"
    keyboard = [
        [InlineKeyboardButton("Создать розыгрыш", callback_data="admin_new")],
        [InlineKeyboardButton("Список розыгрышей", callback_data="admin_list")],
        [InlineKeyboardButton("Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("Назад", callback_data="cmd_start")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if message:
        message.edit_text(text, reply_markup=markup)
    else:
        update.message.reply_text(text, reply_markup=markup)


def new_giveaway(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return

    if len(context.args) < 2:
        update.message.reply_text("Использование: /new <название> <победителей> [часы] [описание]\n\nДобавьте 'sub' в конце для проверки подписки")
        return

    try:
        name = context.args[0]
        winners = int(context.args[1])
        hours = int(context.args[2]) if len(context.args) > 2 and context.args[2].isdigit() else 24

        require_sub = 0
        description_parts = []
        start_index = 3 if len(context.args) > 2 and context.args[2].isdigit() else 2
        for i in range(start_index, len(context.args)):
            if context.args[i].lower() == "sub":
                require_sub = 1
            else:
                description_parts.append(context.args[i])

        description = " ".join(description_parts) if description_parts else "Розыгрыш"

        giveaway_id = db.create_giveaway(name, description, winners, hours, str(CHANNEL_ID), require_sub)
        if not giveaway_id:
            update.message.reply_text("Ошибка создания")
            return

        giveaway_info = db.get_giveaway_info(giveaway_id)
        start_date = giveaway_info[4]
        end_date = giveaway_info[5]
        progress = create_progress_bar(start_date, end_date)

        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Участвовать", callback_data=f"join_{giveaway_id}")]])
        sub_text = "\n\nТребуется подписка на канал!" if require_sub == 1 else ""

        message = context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=(
                f"НОВЫЙ РОЗЫГРЫШ!\n\n"
                f"{name}\n"
                f"{description}\n\n"
                f"Победителей: {winners}\n"
                f"Завершится: {datetime.fromisoformat(end_date).strftime('%d.%m.%Y в %H:%M')}\n\n"
                f"{progress}\n"
                f"{format_time_left(end_date)}"
                f"{sub_text}\n\n"
                f"Нажмите кнопку!"
            ),
            reply_markup=markup
        )
        db.update_message_id(giveaway_id, message.message_id)
        update.message.reply_text(f"Розыгрыш создан! ID: {giveaway_id}" + ("\n\nПроверка подписки: ВКЛ" if require_sub == 1 else ""))
    except Exception as e:
        update.message.reply_text("Ошибка: " + str(e))


def list_giveaways_cmd(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id
    if not is_admin(user_id):
        if message:
            message.edit_text("Нет прав")
        else:
            update.message.reply_text("Нет прав")
        return

    giveaways = db.get_active_giveaways()
    if not giveaways:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_admin")]])
        if message:
            message.edit_text("Нет розыгрышей", reply_markup=markup)
        else:
            update.message.reply_text("Нет розыгрышей", reply_markup=markup)
        return

    text = "Активные розыгрыши:\n\n"
    for g in giveaways:
        gid, name, winners, end_date, require_sub = g
        giveaway_info = db.get_giveaway_info(gid)
        start_date = giveaway_info[4] if giveaway_info else datetime.now().isoformat()
        participants = db.get_participants_count(gid)
        time_left = format_time_left(end_date)
        progress = create_progress_bar(start_date, end_date)
        text += f"ID: {gid}\n{name}\nУчастников: {participants}\n{progress} {time_left}\n------\n"

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="cmd_admin")]])
    if message:
        message.edit_text(text, reply_markup=markup)
    else:
        update.message.reply_text(text, reply_markup=markup)


def finish_giveaway(bot, giveaway_id, message=None):
    try:
        giveaway_info = db.get_giveaway_info(giveaway_id)
        if not giveaway_info:
            if message:
                message.reply_text("Не найден")
            return

        participants = db.get_participants(giveaway_id)
        if not participants:
            db.end_giveaway(giveaway_id)
            if message:
                message.reply_text("Нет участников")
            return

        winner_count = min(giveaway_info[3], len(participants))
        winners = random.sample(participants, winner_count)

        winners_text = "ПОБЕДИТЕЛИ!\n\n"
        for i, winner_id in enumerate(winners, 1):
            try:
                user = bot.get_chat(winner_id)
                username = f"@{user.username}" if user.username else user.first_name
                winners_text += f"{i}. {username}\n"
            except Exception:
                winners_text += f"{i}. ID: {winner_id}\n"

        db.end_giveaway(giveaway_id)

        try:
            bot.send_message(chat_id=CHANNEL_ID, text=winners_text)
        except Exception:
            pass

        if message:
            message.reply_text("Завершен!\n\n" + winners_text)

        logger.info(f"Giveaway {giveaway_id} finished")
    except Exception as e:
        logger.error(f"Error finishing giveaway {giveaway_id}: {e}")


def end_giveaway(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if not context.args:
        update.message.reply_text("Использование: /end <id>")
        return
    try:
        giveaway_id = int(context.args[0])
        finish_giveaway(context.bot, giveaway_id, update.message)
    except Exception as e:
        update.message.reply_text("Ошибка: " + str(e))


def auto_finish_thread(bot):
    while True:
        try:
            giveaways_to_finish = db.get_giveaways_to_finish()
            for gid in giveaways_to_finish:
                finish_giveaway(bot, gid)
            time.sleep(60)
        except Exception as e:
            logger.error("Auto-finish error: " + str(e))
            time.sleep(60)


def stats(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if not context.args:
        update.message.reply_text("Использование: /stats <id>")
        return
    try:
        giveaway_id = int(context.args[0])
        giveaway_info = db.get_giveaway_info(giveaway_id)
        if not giveaway_info:
            update.message.reply_text("Не найден")
            return

        _, name, description, winners, start_date, end_date, is_active, _, _, _, require_sub = giveaway_info
        participants_count = db.get_participants_count(giveaway_id)
        status = "Активен" if is_active == 1 else "Завершен"
        time_left = format_time_left(end_date)
        progress = create_progress_bar(start_date, end_date)
        sub_text = "ВКЛ" if require_sub == 1 else "ВЫКЛ"

        start_dt = datetime.fromisoformat(start_date)
        end_dt = datetime.fromisoformat(end_date)
        text = (
            f"Статистика #{giveaway_id}\n\n"
            f"{name}\n"
            f"{description}\n"
            f"Победителей: {winners}\n"
            f"Участников: {participants_count}\n"
            f"Статус: {status}\n"
            f"Проверка подписки: {sub_text}\n\n"
            f"{progress} {time_left}\n\n"
            f"{start_dt.strftime('%d.%m %H:%M')} - {end_dt.strftime('%d.%m %H:%M')}"
        )
        update.message.reply_text(text)
    except Exception as e:
        update.message.reply_text("Ошибка: " + str(e))


def participants_cmd(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if not context.args:
        update.message.reply_text("Использование: /participants <id>")
        return
    try:
        giveaway_id = int(context.args[0])
        participants = db.get_participants_with_info(giveaway_id)
        if not participants:
            update.message.reply_text("Нет участников")
            return

        giveaway_info = db.get_giveaway_info(giveaway_id)
        name = giveaway_info[1] if giveaway_info else f"#{giveaway_id}"

        text = f"Участники '{name}'\nВсего: {len(participants)}\n\n"
        for i, (user_id, username, first_name, is_banned, join_date) in enumerate(participants[:50], 1):
            status = "BAN" if is_banned == 1 else "OK"
            username_str = f"@{username}" if username else "нет"
            text += f"{i}. {status} {first_name} ({username_str}) - {user_id}\n"
        if len(participants) > 50:
            text += f"\n...и еще {len(participants) - 50}"
        update.message.reply_text(text)
    except Exception as e:
        update.message.reply_text("Ошибка: " + str(e))


def remove_participant(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if len(context.args) < 2:
        update.message.reply_text("Использование: /remove <giveaway_id> <user_id>")
        return
    try:
        giveaway_id = int(context.args[0])
        user_id = int(context.args[1])
        if db.remove_participant(giveaway_id, user_id):
            update.message.reply_text(f"Участник {user_id} удален из {giveaway_id}")
        else:
            update.message.reply_text("Не найден")
    except Exception as e:
        update.message.reply_text("Ошибка: " + str(e))


def ban_user(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if len(context.args) < 2:
        update.message.reply_text("Использование: /ban <user_id> <причина> [дней]")
        return
    try:
        user_id = int(context.args[0])
        if len(context.args) > 2 and context.args[-1].isdigit():
            days = int(context.args[-1])
            reason = " ".join(context.args[1:-1])
        else:
            days = 30
            reason = " ".join(context.args[1:])
        admin_id = update.effective_user.id

        if db.ban_user(user_id, admin_id, reason, days):
            try:
                context.bot.send_message(
                    chat_id=user_id,
                    text=f"ВЫ ЗАБАНЕНЫ!\n\nПричина: {reason}\nСрок: {days} дней"
                )
            except Exception:
                pass
            update.message.reply_text(f"Пользователь {user_id} забанен\nПричина: {reason}\nСрок: {days} дней")
        else:
            update.message.reply_text("Ошибка")
    except Exception as e:
        update.message.reply_text("Ошибка: " + str(e))


def unban_user(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    if not context.args:
        update.message.reply_text("Использование: /unban <user_id>")
        return
    try:
        user_id = int(context.args[0])
        if db.unban_user(user_id):
            try:
                context.bot.send_message(chat_id=user_id, text="Вы разбанены!")
            except Exception:
                pass
            update.message.reply_text(f"Пользователь {user_id} разбанен")
        else:
            update.message.reply_text("Ошибка")
    except Exception as e:
        update.message.reply_text("Ошибка: " + str(e))


def banned_list(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    banned_users = db.get_banned_users()
    if not banned_users:
        update.message.reply_text("Нет забаненных")
        return

    text = f"Забаненные ({len(banned_users)})\n\n"
    for user in banned_users[:30]:
        user_id, username, first_name, reason, ban_date = user
        date_str = datetime.fromisoformat(ban_date).strftime("%d.%m.%Y") if ban_date else "неизвестно"
        username_str = f"@{username}" if username else "нет"
        text += f"{first_name} ({username_str}) - {user_id}\n{reason}\nДата: {date_str}\n------\n"
    if len(banned_users) > 30:
        text += f"\n...и еще {len(banned_users) - 30}"
    update.message.reply_text(text)


def check_multi(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    threshold = int(context.args[0]) if context.args and context.args[0].isdigit() else 2
    suspicious_ips = db.get_suspicious_ips(threshold)
    if not suspicious_ips:
        update.message.reply_text("Подозрительных IP не найдено")
        return

    text = f"Подозрительные IP (порог {threshold})\n\n"
    for ip_hash, user_count, last_seen in suspicious_ips[:30]:
        users = db.get_users_by_ip(ip_hash)
        text += f"IP: {ip_hash}\nПользователей: {user_count}\nПоследний раз: {last_seen}\n"
        for user_id, username, first_name, joined_date in users[:5]:
            username_str = f"@{username}" if username else "нет"
            text += f"- {first_name} ({username_str}) {user_id}\n"
        if len(users) > 5:
            text += f"...и еще {len(users) - 5}\n"
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
        user_id = int(context.args[0])
        info = db.get_verification_info(user_id)
        if not info:
            update.message.reply_text("Пользователь не найден")
            return
        is_verified, verification_date, verification_method, verification_attempts = info
        text = (
            f"Информация о проверке #{user_id}\n\n"
            f"Верифицирован: {'да' if is_verified else 'нет'}\n"
            f"Дата: {verification_date or 'нет'}\n"
            f"Метод: {verification_method or 'нет'}\n"
            f"Попыток: {verification_attempts}"
        )
        update.message.reply_text(text)
    except Exception as e:
        update.message.reply_text("Ошибка: " + str(e))


def button_handler(update, context):
    query = update.callback_query
    query.answer()
    data = query.data

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
        giveaway_id = int(data.split("_")[1])
        user_id = update.effective_user.id

        if db.is_banned(user_id):
            query.answer("❌ Вы забанены", show_alert=True)
            return

        is_verified = db.is_verified(user_id)
        if not is_verified:
            query.answer("🔐 Требуется верификация", show_alert=True)
            context.bot.send_message(
                chat_id=user_id,
                text="⚠️ Для участия в розыгрышах нужно сначала пройти проверку.\n"
                     "Напишите /verify в личном чате со мной или нажмите кнопку «Пройти проверку» в главном меню."
            )
            return

        giveaway_info = db.get_giveaway_info(giveaway_id)
        if not giveaway_info or giveaway_info[6] != 1:
            query.answer("❌ Розыгрыш не найден или завершён", show_alert=True)
            return

        require_sub = giveaway_info[10]
        channel_id = giveaway_info[8]

        if require_sub == 1 and not check_subscription(context.bot, user_id, channel_id):
            query.answer("📢 Подпишитесь на канал, чтобы участвовать", show_alert=True)
            return

        success = db.add_participant(giveaway_id, user_id, context.user_data.get("referrer"))
        if success:
            query.answer("✅ Вы успешно участвуете в розыгрыше!", show_alert=True)
            context.bot.send_message(
                chat_id=user_id,
                text=f"🎉 Вы участвуете в розыгрыше «{giveaway_info[1]}»!\nУдачи!"
            )
        else:
            if user_id in db.get_participants(giveaway_id):
                query.answer("ℹ️ Вы уже участвуете в этом розыгрыше", show_alert=True)
            else:
                query.answer("❌ Ошибка при добавлении. Попробуйте позже.", show_alert=True)


def error_handler(update, context):
    logger.error("Error: %s", context.error)


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

    dp.add_handler(CallbackQueryHandler(button_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

    dp.add_error_handler(error_handler)

    threading.Thread(target=auto_finish_thread, args=(updater.bot,), daemon=True).start()

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
