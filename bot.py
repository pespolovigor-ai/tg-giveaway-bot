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
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, CallbackContext, Filters
from telegram.error import BadRequest

BOT_TOKEN = os.getenv('BOT_TOKEN', '8458068573:AAHaKHcWQZOOmTu-z2wu-7kbX8MdhonkS_M')
ADMIN_IDS = [5207853162, 5406117718]
CHANNEL_ID = "@sportgagarinmolodezh"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_name='giveaway.db'):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()

    def create_tables(self):
        self.cursor.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
            joined_date TEXT NOT NULL, is_verified INTEGER DEFAULT 0, verification_date TEXT,
            verification_method TEXT, is_banned INTEGER DEFAULT 0, ban_reason TEXT,
            banned_date TEXT, ip_hash TEXT, last_activity TEXT, verification_attempts INTEGER DEFAULT 0)""")

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS verification_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            verification_type TEXT NOT NULL, success INTEGER NOT NULL,
            attempt_date TEXT NOT NULL, ip_hash TEXT)""")

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS ban_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            admin_id INTEGER, reason TEXT, ban_date TEXT NOT NULL, unban_date TEXT)""")

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS ip_addresses (
            ip_hash TEXT PRIMARY KEY, user_count INTEGER DEFAULT 1,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""")

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT,
            winner_count INTEGER DEFAULT 1, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            is_active INTEGER DEFAULT 1, message_id INTEGER, channel_id TEXT,
            auto_finish INTEGER DEFAULT 1, require_subscription INTEGER DEFAULT 0)""")

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS participants (
            giveaway_id INTEGER NOT NULL, user_id INTEGER NOT NULL, join_date TEXT NOT NULL,
            is_valid INTEGER DEFAULT 1, referred_by INTEGER, bonus_entries INTEGER DEFAULT 0,
            PRIMARY KEY (giveaway_id, user_id))""")

        self.cursor.execute("""CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL, giveaway_id INTEGER NOT NULL,
            referral_date TEXT NOT NULL, UNIQUE(referrer_id, referred_id, giveaway_id))""")

        self.conn.commit()

    def add_user(self, user_id, username, first_name, last_name=""):
        try:
            self.cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
            exists = self.cursor.fetchone()
            current_time = datetime.now().isoformat()
            if exists:
                self.cursor.execute("UPDATE users SET username = ?, first_name = ?, last_name = ?, last_activity = ? WHERE user_id = ?",
                    (username, first_name, last_name, current_time, user_id))
            else:
                self.cursor.execute("INSERT INTO users (user_id, username, first_name, last_name, joined_date, last_activity, is_verified) VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (user_id, username, first_name, last_name, current_time, current_time))
            self.conn.commit()
            return True
        except:
            return False

    def verify_user(self, user_id, method="captcha", ip_hash=None):
        try:
            current_time = datetime.now().isoformat()
            self.cursor.execute("UPDATE users SET is_verified = 1, verification_date = ?, verification_method = ?, verification_attempts = verification_attempts + 1 WHERE user_id = ?",
                (current_time, method, user_id))
            self.cursor.execute("INSERT INTO verification_history (user_id, verification_type, success, attempt_date, ip_hash) VALUES (?, ?, 1, ?, ?)",
                (user_id, method, current_time, ip_hash))
            self.conn.commit()
            self.cursor.execute('SELECT is_verified FROM users WHERE user_id = ?', (user_id,))
            result = self.cursor.fetchone()
            return result and result[0] == 1
        except:
            self.conn.rollback()
            return False

    def is_verified(self, user_id):
        try:
            self.cursor.execute('SELECT is_verified FROM users WHERE user_id = ?', (user_id,))
            result = self.cursor.fetchone()
            return result and result[0] == 1
        except:
            return False

    def record_verification_attempt(self, user_id, success=False, method="captcha", ip_hash=None):
        try:
            current_time = datetime.now().isoformat()
            self.cursor.execute('UPDATE users SET verification_attempts = verification_attempts + 1 WHERE user_id = ?', (user_id,))
            self.cursor.execute("INSERT INTO verification_history (user_id, verification_type, success, attempt_date, ip_hash) VALUES (?, ?, ?, ?, ?)",
                (user_id, method, 1 if success else 0, current_time, ip_hash))
            self.conn.commit()
            return True
        except:
            return False

    def get_verification_info(self, user_id):
        try:
            self.cursor.execute('SELECT is_verified, verification_date, verification_method, verification_attempts FROM users WHERE user_id = ?', (user_id,))
            return self.cursor.fetchone()
        except:
            return None

    def get_verification_history(self, user_id, limit=10):
        try:
            self.cursor.execute("SELECT verification_type, success, attempt_date, ip_hash FROM verification_history WHERE user_id = ? ORDER BY attempt_date DESC LIMIT ?",
                (user_id, limit))
            return self.cursor.fetchall()
        except:
            return []

    def update_user_activity(self, user_id):
        try:
            self.cursor.execute('UPDATE users SET last_activity = ? WHERE user_id = ?', (datetime.now().isoformat(), user_id))
            self.conn.commit()
        except:
            pass

    def ban_user(self, user_id, admin_id, reason="Нарушение", days=30):
        try:
            current_time = datetime.now()
            unban_date = current_time + timedelta(days=days)
            self.cursor.execute("INSERT INTO ban_list (user_id, admin_id, reason, ban_date, unban_date) VALUES (?, ?, ?, ?, ?)",
                (user_id, admin_id, reason, current_time.isoformat(), unban_date.isoformat()))
            self.cursor.execute('UPDATE users SET is_banned = 1, ban_reason = ?, banned_date = ? WHERE user_id = ?',
                (reason, current_time.isoformat(), user_id))
            self.cursor.execute('UPDATE participants SET is_valid = 0 WHERE user_id = ?', (user_id,))
            self.conn.commit()
            return True
        except:
            return False

    def unban_user(self, user_id):
        try:
            self.cursor.execute('UPDATE users SET is_banned = 0, ban_reason = NULL, banned_date = NULL WHERE user_id = ?', (user_id,))
            self.conn.commit()
            return True
        except:
            return False

    def is_banned(self, user_id):
        try:
            self.cursor.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,))
            result = self.cursor.fetchone()
            return result and result[0] == 1
        except:
            return False

    def get_ban_info(self, user_id):
        try:
            self.cursor.execute('SELECT ban_reason, banned_date FROM users WHERE user_id = ? AND is_banned = 1', (user_id,))
            return self.cursor.fetchone()
        except:
            return None

    def get_banned_users(self):
        try:
            self.cursor.execute('SELECT user_id, username, first_name, ban_reason, banned_date FROM users WHERE is_banned = 1 ORDER BY banned_date DESC')
            return self.cursor.fetchall()
        except:
            return []

    def add_ip_address(self, user_id, ip_address):
        try:
            ip_hash = hashlib.sha256(ip_address.encode()).hexdigest()[:32]
            current_time = datetime.now().isoformat()
            self.cursor.execute('UPDATE users SET ip_hash = ? WHERE user_id = ?', (ip_hash, user_id))
            self.cursor.execute('SELECT user_count FROM ip_addresses WHERE ip_hash = ?', (ip_hash,))
            exists = self.cursor.fetchone()
            if exists:
                self.cursor.execute('UPDATE ip_addresses SET user_count = user_count + 1, last_seen = ? WHERE ip_hash = ?', (current_time, ip_hash))
            else:
                self.cursor.execute('INSERT INTO ip_addresses (ip_hash, user_count, first_seen, last_seen) VALUES (?, 1, ?, ?)', (ip_hash, current_time, current_time))
            self.conn.commit()
            return ip_hash
        except:
            return None

    def get_suspicious_ips(self, threshold=2):
        try:
            self.cursor.execute('SELECT ip_hash, user_count, last_seen FROM ip_addresses WHERE user_count >= ? ORDER BY user_count DESC', (threshold,))
            return self.cursor.fetchall()
        except:
            return []

    def get_users_by_ip(self, ip_hash):
        try:
            self.cursor.execute('SELECT user_id, username, first_name, joined_date FROM users WHERE ip_hash = ? ORDER BY joined_date', (ip_hash,))
            return self.cursor.fetchall()
        except:
            return []

    def check_multiple_accounts(self, user_id):
        try:
            self.cursor.execute('SELECT user_id FROM users WHERE ip_hash = (SELECT ip_hash FROM users WHERE user_id = ?) AND user_id != ?', (user_id, user_id))
            return [row[0] for row in self.cursor.fetchall()]
        except:
            return []

    def create_giveaway(self, name, description, winners, hours, channel_id, require_sub=0):
        try:
            start_date = datetime.now()
            end_date = start_date + timedelta(hours=hours)
            self.cursor.execute("INSERT INTO giveaways (name, description, winner_count, start_date, end_date, is_active, channel_id, auto_finish, require_subscription) VALUES (?, ?, ?, ?, ?, 1, ?, 1, ?)",
                (name, description, winners, start_date.isoformat(), end_date.isoformat(), channel_id, require_sub))
            self.conn.commit()
            return self.cursor.lastrowid
        except:
            return None

    def update_message_id(self, giveaway_id, message_id):
        try:
            self.cursor.execute('UPDATE giveaways SET message_id = ? WHERE id = ?', (message_id, giveaway_id))
            self.conn.commit()
        except:
            pass

    def add_participant(self, giveaway_id, user_id, referred_by=None):
        try:
            current_time = datetime.now().isoformat()
            self.cursor.execute('INSERT INTO participants (giveaway_id, user_id, join_date, referred_by) VALUES (?, ?, ?, ?)',
                (giveaway_id, user_id, current_time, referred_by))
            if referred_by:
                try:
                    self.cursor.execute('INSERT INTO referrals (referrer_id, referred_id, giveaway_id, referral_date) VALUES (?, ?, ?, ?)',
                        (referred_by, user_id, giveaway_id, current_time))
                    self.cursor.execute('UPDATE participants SET bonus_entries = bonus_entries + 1 WHERE giveaway_id = ? AND user_id = ?',
                        (giveaway_id, referred_by))
                except:
                    pass
            self.conn.commit()
            return True
        except:
            return False

    def get_referral_count(self, user_id, giveaway_id):
        try:
            self.cursor.execute('SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND giveaway_id = ?', (user_id, giveaway_id))
            return self.cursor.fetchone()[0]
        except:
            return 0

    def get_bonus_entries(self, user_id, giveaway_id):
        try:
            self.cursor.execute('SELECT bonus_entries FROM participants WHERE giveaway_id = ? AND user_id = ?', (giveaway_id, user_id))
            result = self.cursor.fetchone()
            return result[0] if result else 0
        except:
            return 0

    def get_top_referrers(self, limit=10):
        try:
            self.cursor.execute("""SELECT r.referrer_id, u.username, u.first_name, COUNT(r.referred_id) as ref_count
                FROM referrals r LEFT JOIN users u ON r.referrer_id = u.user_id
                GROUP BY r.referrer_id ORDER BY ref_count DESC LIMIT ?""", (limit,))
            return self.cursor.fetchall()
        except:
            return []

    def remove_participant(self, giveaway_id, user_id):
        try:
            self.cursor.execute('UPDATE participants SET is_valid = 0 WHERE giveaway_id = ? AND user_id = ?', (giveaway_id, user_id))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except:
            return False

    def get_active_giveaways(self):
        try:
            self.cursor.execute('SELECT id, name, winner_count, end_date, require_subscription FROM giveaways WHERE is_active = 1 ORDER BY end_date')
            return self.cursor.fetchall()
        except:
            return []

    def get_giveaway_info(self, giveaway_id):
        try:
            self.cursor.execute('SELECT * FROM giveaways WHERE id = ?', (giveaway_id,))
            return self.cursor.fetchone()
        except:
            return None

    def get_participants(self, giveaway_id, valid_only=True):
        try:
            if valid_only:
                self.cursor.execute('SELECT user_id FROM participants WHERE giveaway_id = ? AND is_valid = 1', (giveaway_id,))
            else:
                self.cursor.execute('SELECT user_id FROM participants WHERE giveaway_id = ?', (giveaway_id,))
            return [row[0] for row in self.cursor.fetchall()]
        except:
            return []

    def get_participants_with_info(self, giveaway_id):
        try:
            self.cursor.execute("SELECT p.user_id, u.username, u.first_name, u.is_banned, p.join_date FROM participants p LEFT JOIN users u ON p.user_id = u.user_id WHERE p.giveaway_id = ? AND p.is_valid = 1 ORDER BY p.join_date",
                (giveaway_id,))
            return self.cursor.fetchall()
        except:
            return []

    def get_participants_count(self, giveaway_id):
        try:
            self.cursor.execute('SELECT COUNT(*) FROM participants WHERE giveaway_id = ? AND is_valid = 1', (giveaway_id,))
            return self.cursor.fetchone()[0]
        except:
            return 0

    def end_giveaway(self, giveaway_id):
        try:
            self.cursor.execute('UPDATE giveaways SET is_active = 0 WHERE id = ?', (giveaway_id,))
            self.conn.commit()
            return True
        except:
            return False

    def get_giveaways_to_finish(self):
        try:
            current_time = datetime.now().isoformat()
            self.cursor.execute('SELECT id FROM giveaways WHERE is_active = 1 AND auto_finish = 1 AND end_date <= ?', (current_time,))
            return [row[0] for row in self.cursor.fetchall()]
        except:
            return []

db = Database()
captcha_storage = {}

def generate_captcha():
    a = random.randint(1, 10)
    b = random.randint(1, 10)
    operations = ['+', '-', '*']
    operation = random.choice(operations)
    if operation == '+':
        answer = a + b
        question = str(a) + " + " + str(b)
    elif operation == '-':
        answer = a - b
        question = str(a) + " - " + str(b)
    else:
        answer = a * b
        question = str(a) + " x " + str(b)
    return question, str(answer)

def extract_ip_from_request(update):
    user = update.effective_user
    return str(user.id) + "." + str(hash(str(user.id)) % 255) + "." + str(hash(user.username or '') % 255)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def check_subscription(bot, user_id, channel_id):
    try:
        member = bot.get_chat_member(channel_id, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def format_time_left(end_date):
    try:
        end = datetime.fromisoformat(end_date)
        now = datetime.now()
        diff = end - now
        if diff.total_seconds() <= 0:
            return "Завершен"
        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60
        if days > 0:
            return str(days) + "д " + str(hours) + "ч"
        elif hours > 0:
            return str(hours) + "ч " + str(minutes) + "мин"
        else:
            return str(minutes) + "мин"
    except:
        return "Неизвестно"

def create_progress_bar(end_date, length=10):
    try:
        end = datetime.fromisoformat(end_date)
        now = datetime.now()
        diff = end - now
        if diff.total_seconds() <= 0:
            return "[" + "█" * length + "]"
        total_hours = 24
        hours_left = diff.total_seconds() / 3600
        progress = max(0, min(1, 1 - (hours_left / total_hours)))
        filled = int(progress * length)
        return "[" + "█" * filled + "░" * (length - filled) + "]"
    except:
        return "[░░░░░░░░░░]"

def start(update, context):
    user = update.effective_user
    db.add_user(user.id, user.username or "", user.first_name, user.last_name or "")
    db.update_user_activity(user.id)
    try:
        ip = extract_ip_from_request(update)
        db.add_ip_address(user.id, ip)
    except:
        pass
    if db.is_banned(user.id):
        update.message.reply_text("Вы забанены")
        return
    if context.args and context.args[0].startswith('ref_'):
        try:
            parts = context.args[0].split('_')
            if len(parts) == 3:
                giveaway_id = int(parts[1])
                referrer_id = int(parts[2])
                context.user_data['referrer'] = referrer_id
                context.user_data['giveaway'] = giveaway_id
                update.message.reply_text("Привет! Сначала пройдите проверку: /verify")
                return
        except:
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
    text = "Привет, " + user.first_name + "!\n\nБот для розыгрышей\n\nВыберите действие:"
    update.message.reply_text(text, reply_markup=markup)

def verify(update, context):
    if update.message.chat.type != 'private':
        update.message.reply_text("Только в личных сообщениях!")
        return
    user_id = update.effective_user.id
    if db.is_banned(user_id):
        update.message.reply_text("Вы забанены")
        return
    if db.is_verified(user_id):
        keyboard = [[InlineKeyboardButton("Главное меню", callback_data="cmd_start")]]
        markup = InlineKeyboardMarkup(keyboard)
        update.message.reply_text("Вы уже верифицированы!", reply_markup=markup)
        return
    question, answer = generate_captcha()
    ip = extract_ip_from_request(update)
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:32]
    captcha_storage[user_id] = {'answer': answer, 'attempts': 0, 'time': datetime.now(), 'ip_hash': ip_hash}
    update.message.reply_text("Пройдите проверку\n\nРешите: " + question + " = ?\n\nОтправьте ответ числом.")

def handle_text(update, context):
    if update.message.chat.type != 'private':
        return
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if db.is_banned(user_id):
        return
    if user_id in captcha_storage:
        captcha = captcha_storage[user_id]
        if datetime.now() - captcha['time'] > timedelta(minutes=5):
            update.message.reply_text("Время вышло. /verify")
            db.record_verification_attempt(user_id, success=False, ip_hash=captcha.get('ip_hash'))
            del captcha_storage[user_id]
            return
        if text == captcha['answer']:
            ip_hash = captcha.get('ip_hash')
            success = db.verify_user(user_id, method="captcha", ip_hash=ip_hash)
            if success:
                del captcha_storage[user_id]
                multi_accounts = db.check_multiple_accounts(user_id)
                if multi_accounts:
                    update.message.reply_text("Обнаружены мультиаккаунты.")
                keyboard = [[InlineKeyboardButton("Главное меню", callback_data="cmd_start")]]
                markup = InlineKeyboardMarkup(keyboard)
                update.message.reply_text("Проверка пройдена!\n\nТеперь можете участвовать!", reply_markup=markup)
            else:
                update.message.reply_text("Ошибка! Попробуйте /verify")
        else:
            captcha['attempts'] += 1
            db.record_verification_attempt(user_id, success=False, ip_hash=captcha.get('ip_hash'))
            if captcha['attempts'] >= 3:
                update.message.reply_text("Попытки закончились. /verify")
                del captcha_storage[user_id]
            else:
                left = 3 - captcha['attempts']
                update.message.reply_text("Неверно. Осталось: " + str(left))

def my_referrals(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id

    if db.is_banned(user_id):
        if message:
            message.edit_text("Вы забанены")
        else:
            update.message.reply_text("Вы забанены")
        return

    if not db.is_verified(user_id):
        keyboard = [[InlineKeyboardButton("Пройти проверку", callback_data="cmd_verify")],
                    [InlineKeyboardButton("Назад", callback_data="cmd_start")]]
        markup = InlineKeyboardMarkup(keyboard)
        if message:
            message.edit_text("Сначала пройдите проверку", reply_markup=markup)
        else:
            update.message.reply_text("Сначала пройдите проверку: /verify", reply_markup=markup)
        return

    active_giveaways = db.get_active_giveaways()
    if not active_giveaways:
        keyboard = [[InlineKeyboardButton("Назад", callback_data="cmd_start")]]
        markup = InlineKeyboardMarkup(keyboard)
        if message:
            message.edit_text("Нет активных розыгрышей", reply_markup=markup)
        else:
            update.message.reply_text("Нет активных розыгрышей", reply_markup=markup)
        return

    text = "Ваши реферальные ссылки:\n\n"
    for g in active_giveaways:
        gid, name, winners, end_date, require_sub = g
        referral_count = db.get_referral_count(user_id, gid)
        bonus_entries = db.get_bonus_entries(user_id, gid)
        bot_username = context.bot.get_me().username
        ref_link = "https://t.me/" + bot_username + "?start=ref_" + str(gid) + "_" + str(user_id)
        time_left = format_time_left(end_date)
        text += name + "\n" + ref_link + "\nПриглашено: " + str(referral_count) + "\nБонусов: " + str(bonus_entries) + "\nОсталось: " + time_left + "\n------\n"
    text += "\nОтправьте ссылку друзьям!"

    keyboard = [[InlineKeyboardButton("Назад", callback_data="cmd_start")]]
    markup = InlineKeyboardMarkup(keyboard)

    if message:
        message.edit_text(text, reply_markup=markup)
    else:
        update.message.reply_text(text, reply_markup=markup)

def top_referrers(update, context, message=None):
    top = db.get_top_referrers(10)
    if not top:
        keyboard = [[InlineKeyboardButton("Назад", callback_data="cmd_start")]]
        markup = InlineKeyboardMarkup(keyboard)
        text = "Пока нет рефереров"
        if message:
            message.edit_text(text, reply_markup=markup)
        else:
            update.message.reply_text(text, reply_markup=markup)
        return

    text = "Топ-10 рефереров:\n\n"
    medals = ["", "", ""]
    for i, (user_id, username, first_name, ref_count) in enumerate(top, 1):
        medal = medals[i-1] if i <= 3 else str(i) + "."
        username_str = "@" + username if username else first_name
        text += medal + " " + username_str + " - " + str(ref_count) + "\n"

    keyboard = [[InlineKeyboardButton("Мои рефералы", callback_data="cmd_my_referrals")],
                [InlineKeyboardButton("Назад", callback_data="cmd_start")]]
    markup = InlineKeyboardMarkup(keyboard)

    if message:
        message.edit_text(text, reply_markup=markup)
    else:
        update.message.reply_text(text, reply_markup=markup)

def help_cmd(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id

    text = "Помощь\n\nПользователь:\n/start - Начать\n/verify - Проверка\n/my_referrals - Рефералы\n/top - Топ рефереров\n/help - Помощь\n"

    if is_admin(user_id):
        text += "\nАдмин:\n/new - Создать\n/list - Список\n/end - Завершить\n/stats - Статистика\n/participants - Участники\n/remove - Удалить\n/ban - Забанить\n/unban - Разбанить\n/banned - Забаненные\n/check_multi - Мультиаккаунты\n/verify_info - Инфо\n"

    keyboard = [[InlineKeyboardButton("Назад", callback_data="cmd_start")]]
    markup = InlineKeyboardMarkup(keyboard)

    if message:
        message.edit_text(text, reply_markup=markup)
    else:
        update.message.reply_text(text, reply_markup=markup)

def admin_panel(update, context, message=None):
    user_id = update.effective_user.id if update.effective_user else message.from_user.id

    if not is_admin(user_id):
        if message:
            message.edit_text("Нет прав")
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

    name = context.args[0]
    winners = int(context.args[1])
    hours = int(context.args[2]) if len(context.args) > 2 and context.args[2].isdigit() else 24

    require_sub = 0
    description_parts = []
    for i in range(3 if len(context.args) > 2 and context.args[2].isdigit() else 2, len(context.args)):
        if context.args[i].lower() == 'sub':
            require_sub = 1
        else:
            description_parts.append(context.args[i])

    description = ' '.join(description_parts) if description_parts else "Розыгрыш"

    giveaway_id = db.create_giveaway(name, description, winners, hours, CHANNEL_ID, require_sub)
    if not giveaway_id:
        update.message.reply_text("Ошибка создания")
        return

    end_time = datetime.now() + timedelta(hours=hours)
    progress = create_progress_bar(end_time.isoformat())

    keyboard = [[InlineKeyboardButton("Участвовать", callback_data="join_" + str(giveaway_id))]]
    markup = InlineKeyboardMarkup(keyboard)

    sub_text = "\n\nТребуется подписка на канал!" if require_sub == 1 else ""

    try:
        message = context.bot.send_message(chat_id=CHANNEL_ID, 
            text="НОВЫЙ РОЗЫГРЫШ!\n\n" + name + "\n" + description + "\n\nПобедителей: " + str(winners) + "\nЗавершится: " + end_time.strftime('%d.%m.%Y в %H:%M') + "\n\n" + progress + "\n" + format_time_left(end_time.isoformat()) + sub_text + "\n\nНажмите кнопку!",
            reply_markup=markup)
        db.update_message_id(giveaway_id, message.message_id)
        update.message.reply_text("Розыгрыш создан! ID: " + str(giveaway_id) + ("\n\nПроверка подписки: ВКЛ" if require_sub == 1 else ""))
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
        keyboard = [[InlineKeyboardButton("Назад", callback_data="cmd_admin")]]
        markup = InlineKeyboardMarkup(keyboard)
        if message:
            message.edit_text("Нет розыгрышей", reply_markup=markup)
        else:
            update.message.reply_text("Нет розыгрышей", reply_markup=markup)
        return

    text = "Активные розыгрыши:\n\n"
    for g in giveaways:
        gid, name, winners, end_date, require_sub = g
        participants = db.get_participants_count(gid)
        time_left = format_time_left(end_date)
        progress = create_progress_bar(end_date)
        sub_mark = " " if require_sub == 1 else ""
        text += "ID: " + str(gid) + " " + sub_mark + "\n" + name + "\nУчастников: " + str(participants) + "\n" + progress + " " + time_left + "\n------\n"

    keyboard = [[InlineKeyboardButton("Назад", callback_data="cmd_admin")]]
    markup = InlineKeyboardMarkup(keyboard)

    if message:
        message.edit_text(text, reply_markup=markup)
    else:
        update.message.reply_text(text, reply_markup=markup)

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

def finish_giveaway(bot, giveaway_id, message=None):
    try:
        participants = db.get_participants(giveaway_id)
        if not participants:
            if message:
                message.reply_text("Нет участников")
            return

        giveaway_info = db.get_giveaway_info(giveaway_id)
        if not giveaway_info:
            if message:
                message.reply_text("Не найден")
            return

        winner_count = min(giveaway_info[3], len(participants))
        winners = random.sample(participants, winner_count)

        winners_text = "ПОБЕДИТЕЛИ!\n\n"
        for i, winner_id in enumerate(winners, 1):
            try:
                user = bot.get_chat(winner_id)
                username = "@" + user.username if user.username else user.first_name
                winners_text += str(i) + ". " + username + "\n"
            except:
                winners_text += str(i) + ". ID: " + str(winner_id) + "\n"

        db.end_giveaway(giveaway_id)

        try:
            bot.send_message(chat_id=CHANNEL_ID, text=winners_text)
        except:
            pass

        if message:
            message.reply_text("Завершен!\n\n" + winners_text)

        logger.info("Giveaway " + str(giveaway_id) + " finished automatically")
    except Exception as e:
        logger.error("Error finishing giveaway " + str(giveaway_id) + ": " + str(e))

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
        participants_count = db.get_participants_count(giveaway_id)
        _, name, description, winners, start_date, end_date, is_active, _, _, _, require_sub = giveaway_info
        start = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
        status = "Активен" if is_active == 1 else "Завершен"
        time_left = format_time_left(end_date)
        progress = create_progress_bar(end_date)
        sub_text = "ВКЛ" if require_sub == 1 else "ВЫКЛ"
        text = "Статистика #" + str(giveaway_id) + "\n\n" + name + "\n" + description + "\nПобедителей: " + str(winners) + "\nУчастников: " + str(participants_count) + "\nСтатус: " + status + "\nПроверка подписки: " + sub_text + "\n\n" + progress + " " + time_left + "\n\n" + start.strftime('%d.%m %H:%M') + " - " + end.strftime('%d.%m %H:%M')
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
        name = giveaway_info[1] if giveaway_info else "#" + str(giveaway_id)
        text = "Участники '" + name + "'\nВсего: " + str(len(participants)) + "\n\n"
        for i, (user_id, username, first_name, is_banned, join_date) in enumerate(participants[:50], 1):
            status = "BAN" if is_banned == 1 else "OK"
            username_str = "@" + username if username else "нет"
            text += str(i) + ". " + status + " " + first_name + " (" + username_str + ") - " + str(user_id) + "\n"
        if len(participants) > 50:
            text += "\n...и еще " + str(len(participants) - 50)
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
            update.message.reply_text("Участник " + str(user_id) + " удален из " + str(giveaway_id))
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
            reason = ' '.join(context.args[1:-1])
        else:
            days = 30
            reason = ' '.join(context.args[1:])
        admin_id = update.effective_user.id
        if db.ban_user(user_id, admin_id, reason, days):
            try:
                context.bot.send_message(chat_id=user_id, text="ВЫ ЗАБАНЕНЫ!\n\nПричина: " + reason + "\nСрок: " + str(days) + " дней")
            except:
                pass
            update.message.reply_text("Пользователь " + str(user_id) + " забанен\nПричина: " + reason + "\nСрок: " + str(days) + " дней")
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
            except:
                pass
            update.message.reply_text("Пользователь " + str(user_id) + " разбанен")
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
    text = "Забаненные (" + str(len(banned_users)) + ")\n\n"
    for user in banned_users[:30]:
        user_id, username, first_name, reason, ban_date = user
        ban_dt = datetime.fromisoformat(ban_date) if ban_date else None
        date_str = ban_dt.strftime('%d.%m.%Y') if ban_dt else 'неизвестно'
        username_str = "@" + username if username else "нет"
        text += first_name + " (" + username_str + ") - " + str(user_id) + "\n" + reason + "\nДата: " + date_str + "\n------\n"
    if len(banned_users) > 30:
        text += "\n...и еще " + str(len(banned_users) - 30)
    update.message.reply_text(text)

def check_multi(update, context):
    if not is_admin(update.effective_user.id):
        update.message.reply_text("Нет прав")
        return
    threshold = int(context.args[0]) if context.args and context.args[0].isdigit() else 2
    suspicious_ips = d
