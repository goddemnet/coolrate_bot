import logging
import sqlite3
import asyncio
import os
import sys
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram import F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram import Router
from PIL import Image
import base64
import json
import datetime
import qrcode # Added import for qrcode library

# === Настройки ===
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_ID", "").split(",") if id.strip()]
DB_PATH = os.getenv("DB_PATH", "database.sqlite")

# === Логирование ===
logging.basicConfig(level=logging.INFO)

# === Инициализация бота и диспетчера ===
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# === Подключение к базе данных ===
try:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Проверка подключения
    cursor.execute("SELECT 1")
    cursor.fetchone()
    logging.info("Successfully connected to database")
except sqlite3.Error as e:
    logging.error(f"Database connection error: {e}")
    sys.exit(1)

cursor.execute('''CREATE TABLE IF NOT EXISTS points_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT,
    points INTEGER,
    note TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)''')

# Создаем таблицу events если её нет
cursor.execute('''CREATE TABLE IF NOT EXISTS events (
    name TEXT PRIMARY KEY,
    content TEXT,
    date TEXT,
    completed INTEGER DEFAULT 0
)''')

# Создаем таблицу для хранения пригласительных ссылок
cursor.execute('''CREATE TABLE IF NOT EXISTS user_invites (
    user_id INTEGER PRIMARY KEY,
    invite_link TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)''')

# Создаем таблицу users если её нет
cursor.execute('''CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    nickname TEXT UNIQUE,
    real_name TEXT,
    phone TEXT,
    category TEXT,
    active INTEGER DEFAULT 1,
    points INTEGER DEFAULT 0,
    participations INTEGER DEFAULT 0,
    photo_path TEXT,
    registration_date DATETIME DEFAULT CURRENT_TIMESTAMP
)''')

# Проверяем наличие колонки invited_by
cursor.execute("PRAGMA table_info(users)")
columns = cursor.fetchall()
if not any(column[1] == 'invited_by' for column in columns):
    cursor.execute('ALTER TABLE users ADD COLUMN invited_by INTEGER')

# Проверяем наличие колонки registration_date
cursor.execute("PRAGMA table_info(users)")
columns = cursor.fetchall()
if not any(column[1] == 'registration_date' for column in columns):
    cursor.execute('ALTER TABLE users ADD COLUMN registration_date DATETIME')
    cursor.execute('UPDATE users SET registration_date = CURRENT_TIMESTAMP')

conn.commit()

# === FSM Модель ===
class Register(StatesGroup):
    nickname = State()
    real_name = State()
    phone = State()

class UpdateProfile(StatesGroup):
    phone = State()
    real_name = State()
    category = State()

class EventCreation(StatesGroup):
    content = State()


# === Функции для работы с БД ===
def register_user(user_id, nickname, real_name, phone, category):
    cursor.execute("""
        INSERT INTO users 
        (user_id, nickname, real_name, phone, category, registration_date) 
        VALUES (?, ?, ?, ?, ?, datetime('now'))
    """, (user_id, nickname, real_name, phone, category))
    conn.commit()

def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()

def get_user_by_nickname(nickname):
    cursor.execute("SELECT * FROM users WHERE nickname = ?", (nickname,))
    return cursor.fetchone()

def update_user(user_id, field, value):
    cursor.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()

def update_user_photo(nickname, path):
    cursor.execute("UPDATE users SET photo_path = ? WHERE nickname = ?", (path, nickname))
    conn.commit()

async def add_points(nickname, points, note):
    # Получаем user_id пользователя
    cursor.execute("SELECT user_id FROM users WHERE nickname = ?", (nickname,))
    result = cursor.fetchone()
    if result:
        user_id = result[0]
        # Обновляем очки
        cursor.execute("UPDATE users SET points = points + ?, participations = participations + 1 WHERE nickname = ?",
                       (points, nickname))
        cursor.execute("INSERT INTO points_history (nickname, points, note) VALUES (?, ?, ?)",
                       (nickname, points, note))
        conn.commit()
        # Отправляем уведомление
        try:
            await bot.send_message(user_id, f"Вам начислено {points} баллов\nПримечание: {note}")
        except Exception as e:
            logging.error(f"Error sending points notification: {e}")

def disable_user(nickname):
    cursor.execute("UPDATE users SET active = 0 WHERE nickname = ?", (nickname,))
    conn.commit()

def get_top_users(limit=10, by="points"):
    cursor.execute(f"SELECT nickname, {by}, active FROM users ORDER BY {by} DESC LIMIT ?", (limit,))
    return cursor.fetchall()

# === Middleware для проверки личных сообщений ===
@router.message.middleware()
async def check_private_chat(handler, event: Message, data):
    if event.chat.type != 'private':
        return
    return await handler(event, data)

# === Обработчики ===
@router.message(CommandStart())
async def send_welcome(message: Message):
    user = get_user(message.from_user.id)

    if user:
        buttons = [
            [KeyboardButton(text="Профиль"), KeyboardButton(text="Рейтинг")],
            [KeyboardButton(text="Информация")],
            [KeyboardButton(text="Ближайшие события")],
            [KeyboardButton(text="Мое приглашение")]
        ]
        welcome_text = "Добро пожаловать в систему рейтинга!"
    else:
        buttons = [
            [KeyboardButton(text="Зарегистрироваться")],
            [KeyboardButton(text="Информация")]
        ]
        welcome_text = "Привет! Для использования бота необходимо зарегистрироваться.\n Номер телефона видят только администраторы.\n Используйте настоящее имя что бы другие поняли кто вы.\n"

    markup = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)
    await message.answer(welcome_text, reply_markup=markup)

@router.message(F.text == "Информация")
async def info_during_registration(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        markup = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Зарегистрироваться")], [KeyboardButton(text="Информация")]],
            resize_keyboard=True
        )
        await message.answer(
            "Приветствую! Это тестовый запуск тг-бота с системой рейтинга Гагаринский спот.\n\n"
            "Регистрация и многие функции уже доступны. И скоро список участников пополнится. Места формируются по колличеству очков.\n\n"
            "После тестового периода все очки будут обнулены.\n\n"
            "Контакты:\n"
            "По любым вопросам: @lagfyuj91\n"
            "Тех. сервис, Мероприятия, Чат и другое: @PoGood_72\n"
            "-\nПожалуйста, пишите об ошибках в боте или о своих идеях! Рассмотрим все!",
            reply_markup=markup
        )
        return
    await info(message)

@router.message(F.text == "Зарегистрироваться")
async def start_registration(message: Message, state: FSMContext):
    if get_user(message.from_user.id):
        await message.answer("Вы уже зарегистрированы.")
        return

    try:
        chat_member = await bot.get_chat_member(chat_id=-1002235947486, user_id=message.from_user.id)

        if chat_member.status in ['member', 'administrator', 'creator']:
            await state.clear()
            await state.set_state(Register.nickname)
            await message.answer("Придумайте себе Никнейм\n(Будет отображаться в таблице рейтинга):")
            return
        else:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Подписаться на канал", url="https://t.me/+6cIySVfPrAQ4ZTg0"),
                InlineKeyboardButton(text="✓ Я подписался", callback_data="check_subscription")
            ]])
            await message.answer("Для регистрации необходимо подписаться на наш канал:", reply_markup=markup)

    except Exception as e:
        logging.error(f"Error checking channel membership: {e}")
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Подписаться на канал", url="https://t.me/+6cIySVfPrAQ4ZTg0"),
            InlineKeyboardButton(text="✓ Я подписался", callback_data="check_subscription")
        ]])
        await message.answer("Для регистрации необходимо подписаться на наш канал:", reply_markup=markup)

@router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, state: FSMContext):
    try:
        chat_member = await bot.get_chat_member(chat_id=-1002299467521, user_id=callback.from_user.id)

        # Проверяем статус участника
        is_member = chat_member.status in ['member', 'administrator', 'creator']

        if is_member:
            # Если подписан - переходим к регистрации
            await state.clear()
            await state.set_state(Register.nickname)
            await callback.message.edit_text("Придумайте себе Никнейм\n(Будет отображаться в таблице рейтинга):")
            await callback.answer("✅ Подписка подтверждена! Продолжаем регистрацию.", show_alert=True)
        else:
            # Если не подписан - показываем сообщение
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Подписаться на канал", url="https://t.me/+6cIySVfPrAQ4ZTg0"),
                InlineKeyboardButton(text="✓ Я подписался", callback_data="check_subscription")
            ]])
            await callback.answer("❌ Вы не подписаны на канал. Подпишитесь и нажмите кнопку проверки.", show_alert=True)
            await callback.message.edit_text(
                "⚠️ Для регистрации необходимо подписаться на наш канал:",
                reply_markup=markup
            )
    except Exception as e:
        logging.error(f"Error in check_subscription: {e}")
        await callback.answer("❌ Ошибка при проверке подписки. Попробуйте снова.", show_alert=True)

def is_valid_nickname(nickname: str) -> tuple[bool, str]:
    if not nickname:
        return False, "Никнейм не может быть пустым"

    if len(nickname.strip()) != len(nickname):
        return False, "Никнейм не должен содержать пробелы в начале или конце"

    if " " in nickname:
        return False, "Никнейм не должен содержать пробелы"

    allowed_chars = set('абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯabcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-@.#$%&*+=')
    if not all(char in allowed_chars for char in nickname):
        return False, "Никнейм может содержать только буквы, цифры и специальные символы (_-@.#$%&*+=)"

    return True, ""

@router.message(Register.nickname)
async def get_nickname(message: Message, state: FSMContext):
    nickname = message.text

    # Проверка валидности никнейма
    is_valid, error_message = is_valid_nickname(nickname)
    if not is_valid:
        await message.answer(error_message)
        return

    # Проверка занятости никнейма
    if cursor.execute("SELECT * FROM users WHERE nickname = ?", (nickname,)).fetchone():
        await message.answer("Этот никнейм уже занят. Попробуйте другой.")
        return

    await state.update_data(nickname=nickname)
    await state.set_state(Register.real_name)
    await message.answer("Введите ваше настоящее имя и инициалы:")

@router.message(Register.real_name)
async def get_real_name(message: Message, state: FSMContext):
    await state.update_data(real_name=message.text)
    await state.set_state(Register.phone)
    await message.answer("Введите номер телефона (по желанию, можно пропустить):")


def encode_data(data: dict) -> str:
    """Кодирует данные в безопасный формат для callback"""
    try:
        if not isinstance(data, (str, dict)):
            logging.error(f"Invalid data type for encoding: {type(data)}")
            return ""

        # Преобразуем в JSON если это словарь
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False)

        # Кодируем в base64
        encoded = base64.urlsafe_b64encode(data.encode('utf-8')).decode('utf-8')
        return encoded.rstrip('=')  # Убираем padding
    except Exception as e:
        logging.error(f"Error encoding data: {str(e)}")
        return ""

def decode_data(data: str) -> dict:
    """Декодирует строку из безопасного формата"""
    try:
        if not isinstance(data, str) or not data:
            return {}

        # Добавляем padding
        missing_padding = len(data) % 4
        if missing_padding:
            data += '=' * (4 - missing_padding)

        # Декодируем из base64
        decoded = base64.urlsafe_b64decode(data.encode('utf-8')).decode('utf-8')

        # Парсим JSON
        try:
            return json.loads(decoded)
        except json.JSONDecodeError as je:
            logging.error(f"JSON decode error: {je}")
            return {}

    except Exception as e:
        logging.error(f"Error decoding data: {str(e)}")
        return {}

@router.message(Register.phone)
async def get_phone(message: Message, state: FSMContext):
    phone = message.text if message.text else "Не указан"
    await state.update_data(phone=phone)
    data = await state.get_data()

    # Получаем информацию о приглашении
    try:
        chat_member = await bot.get_chat_member(chat_id=-1002235947486, user_id=message.from_user.id)
        if chat_member and hasattr(chat_member, 'invite_link'):
            # Находим пользователя, создавшего приглашение
            cursor.execute("SELECT user_id FROM user_invites WHERE invite_link = ?", (chat_member.invite_link,))
            inviter = cursor.fetchone()
            if inviter:
                await state.update_data(invited_by=inviter[0])
    except Exception as e:
        logging.error(f"Error getting invite info: {e}")

    # Сохраняем данные во временную таблицу
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS temp_registration (
                user_id INTEGER PRIMARY KEY,
                nickname TEXT,
                real_name TEXT,
                phone TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        # Очищаем старые временные данные
        cursor.execute("DELETE FROM temp_registration WHERE user_id = ?", (message.from_user.id,))

        # Сохраняем новые данные
        cursor.execute(
            "INSERT INTO temp_registration (user_id, nickname, real_name, phone) VALUES (?, ?, ?, ?)",
            (message.from_user.id, data["nickname"], data["real_name"], phone)
        )
        conn.commit()

        # Используем простой callback_data
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Юноши", callback_data=f"reg_cat:1")],
            [InlineKeyboardButton(text="Подростки", callback_data=f"reg_cat:2")],
            [InlineKeyboardButton(text="Взрослые", callback_data=f"reg_cat:3")]
        ])

        await message.answer("Выберите возрастную категорию:", reply_markup=markup)
        await state.clear()

    except Exception as e:
        logging.error(f"Error in registration process: {e}")
        await message.answer("Произошла ошибка при регистрации. Пожалуйста, попробуйте снова.")
        await state.clear()

@router.callback_query(F.data.startswith("reg_cat:"))
async def finalize_registration(callback: CallbackQuery):
    try:
        # Получаем категорию из callback_data
        cat_id = callback.data.split(":")[1]
        category_map = {"1": "Юноши", "2": "Подростки", "3": "Взрослые"}
        category = category_map.get(cat_id)

        if not category:
            await callback.answer("Неверная категория", show_alert=True)
            return

        # Получаем данные из временной таблицы
        cursor.execute(
            "SELECT nickname, real_name, phone FROM temp_registration WHERE user_id = ?", 
            (callback.from_user.id,)
        )
        result = cursor.fetchone()

        if not result:
            await callback.answer("Данные регистрации не найдены. Пожалуйста, начните регистрацию заново.", show_alert=True)
            return

        nickname, real_name, phone = result
        category_map = {"1": "Юноши", "2": "Подростки", "3": "Взрослые"}
        category = category_map.get(cat_id)

        if not category:
            await callback.answer("Неверная категория", show_alert=True)
            return

        # Получаем информацию о приглашении из состояния
        data = await state.get_data()
        invited_by = data.get('invited_by')

        # Обновляем SQL запрос для сохранения информации о приглашении
        cursor.execute("""
            INSERT INTO users (user_id, nickname, real_name, phone, category, invited_by, registration_date)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """, (callback.from_user.id, nickname, real_name, phone, category, invited_by))
        conn.commit()

        buttons = [
            [KeyboardButton(text="Профиль"), KeyboardButton(text="Рейтинг")],
            [KeyboardButton(text="Информация")],
            [KeyboardButton(text="Ближайшие события")],
            [KeyboardButton(text="Мое приглашение")]
        ]
        markup = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)

        await callback.message.answer(
            "Регистрация завершена! Вы можете посмотреть свой профиль через команду или кнопку 'Профиль'.",
            reply_markup=markup
        )
    except Exception as e:
        logging.error(f"Error in registration: {e}")
        try:
            await callback.answer("Произошла ошибка при регистрации", show_alert=True)
        except:
            logging.error("Failed to send error message to user")

async def check_channel_subscription(user_id: int) -> tuple[bool, InlineKeyboardMarkup]:
    """Проверяет подписку на канал и возвращает статус и клавиатуру если не подписан"""
    try:
        chat_member = await bot.get_chat_member(chat_id=-1002299467521, user_id=user_id)
        if chat_member.status in ['left', 'kicked', 'restricted']:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Продолжить", callback_data="check_subscription_general")
            ]])
            return False, markup
        return True, None
    except Exception as e:
        logging.error(f"Error checking channel membership: {e}")
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Продолжить", callback_data="check_subscription_general")
        ]])
        return False, markup

@router.callback_query(F.data == "check_subscription_general")
async def check_subscription_general(callback: CallbackQuery):
    is_subscribed, markup = await check_channel_subscription(callback.from_user.id)
    if not is_subscribed:
        await callback.answer("Вы еще не подписались на канал.", show_alert=True)
        return
    # Перенаправляем на предыдущую команду
    if callback.message.text and "рейтинг" in callback.message.text.lower():
        await show_rating(callback.message)
    else:
        await profile(callback.message, None)
    await callback.message.delete()

@router.message(Command(commands=["профиль"]))
@router.message(F.text == "Профиль")
async def profile(message: Message, state: FSMContext):
    # Проверка подписки
    is_subscribed, markup = await check_channel_subscription(message.from_user.id)
    if not is_subscribed:
        await message.answer(
            "Сначала вы должны подписаться на наш канал https://t.me/+6cIySVfPrAQ4ZTg0",
            reply_markup=markup
        )
        return

    args = message.text.split()
    viewing_own_profile = len(args) <= 1 or args[0] != "/профиль"

    if not viewing_own_profile:
        # Просмотр чужого профиля
        nickname = args[1]
        user = get_user_by_nickname(nickname)
        if not user:
            await message.answer("Пользователь не найден.")
            return
        # Получаем информацию о пользователе из Telegram
        try:
            chat = await bot.get_chat(user[0])
            username = f"@{chat.username}" if chat.username else "не указан"
        except Exception as e:
            logging.error(f"Error getting username: {e}")
            username = "не указан"

        # Форматируем дату регистрации
        try:
            registration_date = user[9].split('.')[0].replace('T', ' ') if user[9] else "Не указана"
        except (IndexError, AttributeError):
            registration_date = "Не указана"

        if message.from_user.id in ADMIN_IDS:
            profile_text = f"Профиль пользователя {nickname}:\nИмя: {user[2]}\nКатегория: {user[4]}\nБаллы: {user[6]}\nУчастий: {user[7]}\n\nTelegram: {username}\nID: {user[0]}\nДата регистрации: {registration_date}"
        else:
            profile_text = f"Профиль пользователя {nickname}:\nИмя: {user[2]}\nКатегория: {user[4]}\nБаллы: {user[6]}\nУчастий: {user[7]}"
    else:
        # Просмотр своего профиля
        user = get_user(message.from_user.id)
        if not user:
            await message.answer("Вы не зарегистрированы. Используйте /start.")
            return
        invites_count = get_invites_count(message.from_user.id)
        profile_text = f"Ваш профиль:\nНикнейм: {user[1]}\nИмя: {user[2]}\nТелефон: {user[3]}\nКатегория: {user[4]}\nБаллы: {user[6]}\nУчастий: {user[7]}\nПригласил: {invites_count}"

    photo_path = user[8] if len(user) > 8 else None
    nickname = user[1]  # Получаем никнейм из объекта user
    buttons = [[InlineKeyboardButton(text="История начислений", callback_data=f"history:{nickname}")]]
    if viewing_own_profile:
        buttons.append([InlineKeyboardButton(text="Обновить данные", callback_data="update_profile")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    if photo_path and os.path.exists(photo_path):
        await message.answer_photo(photo=FSInputFile(photo_path), caption=profile_text, reply_markup=markup)
    else:
        await message.answer(profile_text, reply_markup=markup)

@router.message(F.text.regexp(r"^/профиль_.*"))
async def profile_link(message: Message):
    nickname = message.text.replace("/профиль_", "")
    user = get_user_by_nickname(nickname)
    if not user:
        await message.answer("Пользователь не найден.")
        return
    profile_text = f"Профиль пользователя {nickname}:\nИмя: {user[2]}\nКатегория: {user[4]}\nБаллы: {user[6]}\nУчастий: {user[7]}"
    photo_path = user[8] if len(user) > 8 else None
    if photo_path and os.path.exists(photo_path):
        await message.answer_photo(photo=FSInputFile(photo_path), caption=profile_text)
    else:
        await message.answer(profile_text)

def get_all_users(offset=0, limit=20):
    cursor.execute("SELECT nickname, points, active FROM users ORDER BY points DESC LIMIT ? OFFSET ?", (limit, offset))
    return cursor.fetchall()

def get_total_users():
    cursor.execute("SELECT COUNT(*) FROM users")
    return cursor.fetchone()[0]

@router.message(Command(commands=["рейтинг"]))
@router.message(F.text == "Рейтинг")
async def show_rating(message: Message):
    # Проверка регистрации
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Вы не зарегистрированы. Используйте /start для регистрации.")
        return

    # Проверка подписки
    is_subscribed, markup = await check_channel_subscription(message.from_user.id)
    if not is_subscribed:
        await message.answer(
            "Сначала вы должны подписаться на наш канал https://t.me/+6cIySVfPrAQ4ZTg0",
            reply_markup=markup
        )
        return

    text = "Таблица рейтинга с 🏆 Топ-10 по баллам:\n\nПосмотреть любой профиль <b>/профиль ник</b>\n\n"

    # Показываем топ-10
    top_users = get_top_users()
    for i, (nickname, points, active) in enumerate(top_users, start=1):
        if active:
            text += f"{i}. <a href='/профиль {nickname}'>{nickname}</a> - {points} баллов\n"
        else:
            text += f"{i}. <s>{nickname}</s> - {points} баллов\n"


    # Показываем полный список с пагинацией
    page_users = get_all_users(0, 20)
    total_users = get_total_users()
    max_pages = (total_users - 1) // 20 + 1

    #text = "Таблица рейтинга с 🏆 Топ-10 по баллам:\n\n"
    #for i, (nickname, points, active) in enumerate(page_users, start=1):
    #    if active:
    #        text += f"{i}. <a href='/профиль {nickname}'>{nickname}</a> - {points} баллов\n"
    #    else:
    #        text += f"{i}. <s>{nickname}</s> - {points} баллов\n"

    keyboard = []
    if max_pages > 1:
        keyboard.append([
            InlineKeyboardButton(text="←", callback_data="rating_page:prev:0"),
            InlineKeyboardButton(text="→", callback_data="rating_page:next:0")
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("rating_page:"))
async def handle_rating_pagination(callback: CallbackQuery):
    _, action, current_page = callback.data.split(":")
    current_page = int(current_page)
    total_users = get_total_users()
    max_pages = (total_users - 1) // 20 + 1

    if action == "next" and current_page < max_pages - 1:
        current_page += 1
    elif action == "prev" and current_page > 0:
        current_page -= 1

    users = get_all_users(current_page * 20, 20)
    text = "📊 Полный список участников:\n\n"
    for i, (nickname, points, active) in enumerate(users, start=current_page * 20 + 1):
        if active:
            text += f"{i}. <a href='/профиль {nickname}'>{nickname}</a> - {points} баллов\n"
        else:
            text += f"{i}. <s>{nickname}</s> - {points} баллов\n"

    keyboard = []
    if max_pages > 1:
        keyboard.append([
            InlineKeyboardButton(text="←", callback_data=f"rating_page:prev:{current_page}"),
            InlineKeyboardButton(text="→", callback_data=f"rating_page:next:{current_page}")
        ])

    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)

@router.message(Command(commands=["мой_рейтинг"]))
async def my_rating(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.answer("Вы не зарегистрированы.")
        return

    cursor.execute(
        "SELECT COUNT(*) + 1 FROM users WHERE points > (SELECT points FROM users WHERE user_id = ?)",
        (message.from_user.id,)
    )
    rank = cursor.fetchone()[0]

    await message.answer(
        f"Ваш рейтинг:\nНикнейм: {user[1]}\nБаллы: {user[6]}\nМесто в рейтинге: {rank}"
    )

@router.message(F.text == "Информация")
async def info(message: Message):
    await message.answer(
        "Приветствую! Это тестовый запуск тг-бота с системой рейтинга Гагаринский спот.\n\n"
        "Регистрация и многие функции уже доступны. И скоро список участников пополнится. Места формируются по колличеству очков.\n\n"
        "После тестового периода все очки будут обнулены.\n\n"
        "Контакты:\n"
        "По любым вопросам: @lagfyuj91\n"
        "Тех. сервис, Мероприятия, Чат и другое: @PoGood_72\n"
        "-\nПожалуйста, пишите об ошибках в боте или о своих идеях! Рассмотрим все!"
    )

def load_events():
    """Загружает события из БД"""
    cursor.execute("SELECT name, content, date, completed FROM events")
    events = {}
    for name, content, date, completed in cursor.fetchall():
        events[name] = {
            "content": content,
            "date": date,
            "completed": bool(completed)
        }
    return events

def save_event(name, content):
    """Сохраняет событие в БД"""
    cursor.execute(
        "INSERT INTO events (name, content, date) VALUES (?, ?, ?)",
        (name, content, datetime.datetime.now().strftime("%d.%m.%Y %H:%M"))
    )
    conn.commit()

def delete_event_db(name):
    """Удаляет событие из БД"""
    cursor.execute("DELETE FROM events WHERE name = ?", (name,))
    conn.commit()

def complete_event_db(name):
    """Отмечает событие как завершенное"""
    cursor.execute("UPDATE events SET completed = 1 WHERE name = ?", (name,))
    conn.commit()

# ЗаЗагружаем события при запуске
EVENTS = load_events()

@router.message(F.text == "Ближайшие события")
async def show_events(message: Message):
    if not EVENTS:
        await message.answer("Нет предстоящих событий")
        return

    buttons = []
    for event_name, event_data in EVENTS.items():
        date = event_data['date'].split()[0]  # Get only the date part
        status_prefix = "✅ Завершено - " if event_data["completed"] else "⚡️ Активное - "
        display_name = f"{status_prefix}{event_name} ({date})"
        buttons.append([InlineKeyboardButton(text=display_name, callback_data=f"event:{event_name}")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Выберите событие:", reply_markup=markup)

@router.callback_query.middleware()
async def check_private_chat_callback(handler, event: CallbackQuery, data):
    if event.message.chat.type != 'private':
        return
    return await handler(event, data)

@router.callback_query(F.data.startswith("event:"))
async def show_event_details(callback: CallbackQuery):
    event_name = callback.data.split(":")[1]
    if event_name in EVENTS:
        event_data = EVENTS[event_name]
        status = "Завершено" if event_data["completed"] else "Активное"

        buttons = []
        if callback.from_user.id in ADMIN_IDS and not event_data["completed"]:
            buttons.append([InlineKeyboardButton(text="Завершить", callback_data=f"complete_event:{event_name}")])
        buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_events")])

        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(
            f"{event_name}\n\nСтатус: {status}\nОт: {event_data['date']}\nСодержание:\n\n{event_data['content']}",
            reply_markup=markup
        )

@router.callback_query(F.data == "back_to_events")
async def back_to_events_list(callback: CallbackQuery):
    await show_events(callback.message)

@router.callback_query(F.data.startswith("complete_event:"))
async def complete_event(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав")
        return

    event_name = callback.data.split(":")[1]
    if event_name in EVENTS:
        EVENTS[event_name]["completed"] = True
        complete_event_db(event_name)
        await show_event_details(callback)
        await callback.answer("Событие помечено как завершенное")

@router.message(Command(commands=["событие"]))
async def add_event(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Используйте: /событие <название>")
        return

    event_name = args[1]
    await state.update_data(eventname=event_name)
    await state.set_state(EventCreation.content)
    await message.answer(f"Введите содержание события \"{event_name}\":")

@router.message(EventCreation.content)
async def add_event_content(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return

    data = await state.get_data()
    event_name = data.get("event_name")
    save_event(event_name, message.text)
    EVENTS[event_name] = {
        "content": message.text,
        "date": datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
        "completed": False
    }

    await message.answer(f"Событие \"{event_name}\" успешно создано!")
    await state.clear()

@router.message(Command(commands=["выдать"]))
async def give_points(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    args = message.text.split()
    if len(args) < 4:
        await message.answer("Используйте: /выдать <ник> <баллы> <примечание>")
        return
    nickname = args[1]
    try:
        points = int(args[2])
    except ValueError:
        await message.answer("Укажите корректное количество баллов.")
        return
    note = " ".join(args[3:])
    if not get_user_by_nickname(nickname):
        await message.answer("Пользователь с таким никнеймом не найден.")
        return
    await add_points(nickname, points, note)
    await message.answer(f"Выдано {points} баллов для {nickname}. Примечание: {note}")

def reset_user_rating(nickname: str):
    """Обнуляет рейтинг пользователя и удаляет историю начислений"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        # Обнуляем рейтинг
        cursor.execute("UPDATE users SET points = 0, participations = 0 WHERE nickname = ?", (nickname,))
        # Удаляем историю начислений
        cursor.execute("DELETE FROM points_history WHERE nickname = ?", (nickname,))
        conn.commit()
    finally:
        conn.close()

def delete_user_by_id_or_nickname(identifier):
    """Полностью удаляет пользователя из базы данных по ID или никнейму"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        # Определяем тип идентификатора и получаем данные пользователя
        try:
            user_id = int(identifier)
            cursor.execute("SELECT nickname, photo_path FROM users WHERE user_id = ?", (user_id,))
        except ValueError:
            cursor.execute("SELECT nickname, photo_path FROM users WHERE nickname = ?", (identifier,))

        result = cursor.fetchone()
        if not result:
            return None

        nickname, photo_path = result

        # Удаляем фото если есть
        if photo_path and os.path.exists(photo_path):
            os.remove(photo_path)

        # Удаляем пользователя и его историю
        cursor.execute("DELETE FROM users WHERE nickname = ?", (nickname,))
        cursor.execute("DELETE FROM points_history WHERE nickname = ?", (nickname,))
        conn.commit()
        return nickname
    finally:
        conn.close()

@router.message(Command(commands=["удалить"]))
async def delete_user_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Используйте: /удалить <ник или id>")
        return

    identifier = args[1]
    nickname = delete_user_by_id_or_nickname(identifier)

    if not nickname:
        await message.answer("Пользователь не найден.")
        return

    await message.answer(f"Пользователь {nickname} полностью удален из системы.")

@router.message(Command(commands=["обнулить"]))
async def reset_rating_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Используйте: /обнулить <ник>")
        return
    nickname = args[1]
    user = get_user_by_nickname(nickname)
    if not user:
        await message.answer("Пользователь не найден.")
        return
    reset_user_rating(nickname)
    await message.answer(f"Рейтинг пользователя {nickname} обнулен, история начислений удалена.")

@router.message(Command(commands=["отключить"]))
async def disable_user_command(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Используйте: /отключить <ник>")
        return
    nickname = args[1]
    if not get_user_by_nickname(nickname):
        await message.answer("Пользователь не найден.")
        return
    disable_user(nickname)
    await message.answer(f"Пользователь {nickname} был отключён и будет зачёркнут в рейтинге.")

@router.message(Command(commands=["обновить_фото"]))
async def update_photo_command(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Используйте: /обновить_фото <никнейм>")
        return
    nickname = args[1]
    if not get_user_by_nickname(nickname):
        await message.answer("Пользователь не найден.")
        return
    await state.update_data(update_photo_nickname=nickname)
    await message.answer("Пришлите вертикальное изображение в формате .png, .jpeg или .jpg")

@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        nickname = data.get('update_photo_nickname')
        if not nickname:
            return

        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        ext = "jpg"
        path = f"photos/{nickname}.{ext}"
        temp_path = f"photos/temp_{nickname}.{ext}"

        os.makedirs("photos", exist_ok=True)

        # Сначала сохраняем во временный файл
        await bot.download_file(file.file_path, destination=temp_path)

        try:
            with Image.open(temp_path) as img:
                width, height = img.size
                if width > height:
                    await message.answer("Пожалуйста, отправьте вертикальное фото (высота должна быть больше ширины)")
                    return

                # Оптимизируем размер
                if width > 1080:
                    new_width = 1080
                    new_height = int(height * (1080 / width))
                    img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

                # Сохраняем оптимизированное фото
                img.save(path, format='JPEG', quality=85, optimize=True)

            # Удаляем старое фото если оно существует
            if os.path.exists(path) and path != temp_path:
                try:
                    os.remove(path)
                except OSError:
                    pass

            update_user_photo(nickname, path)
            await message.answer("Фото успешно обновлено!")

        except Exception as e:
            logging.error(f"Error processing image: {e}")
            await message.answer("Ошибка при обработке изображения.")

        finally:
            # Удаляем временный файл
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

    except Exception as e:
        logging.error(f"Error in handle_photo: {e}")
        await message.answer("Произошла ошибка при обработке фото.")
    finally:
        await state.clear()

@router.callback_query(F.data == "update_profile")
async def update_profile_start(callback: CallbackQuery, state: FSMContext):
    # Создаем новую кнопку отмены
    buttons = [[InlineKeyboardButton(text="Прервать", callback_data="cancel_update")]]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    # Устанавливаем состояние
    await state.set_state(UpdateProfile.phone)

    # Получаем текущий текст/подпись сообщения
    current_text = callback.message.caption if callback.message.caption else callback.message.text

    # Сохраняем данные в состоянии
    await state.update_data(
        original_message_id=callback.message.message_id,
        profile_text=current_text
    )

    # Обновляем сообщение
    if callback.message.photo:
        await callback.message.edit_caption(caption=current_text, reply_markup=markup)
    else:
        await callback.message.edit_text(text=current_text, reply_markup=markup)

    msg = await callback.message.answer("Ваш номер телефона (видит только администратор):")
    await state.update_data(last_message_id=msg.message_id)

@router.message(UpdateProfile.phone)
async def update_profile_phone(message: Message, state: FSMContext):
    update_user(message.from_user.id, "phone", message.text)
    await state.set_state(UpdateProfile.real_name)
    msg = await message.answer("Ваше Имя и Инициалы:")

    # Получаем текущие данные
    data = await state.get_data()
    message_ids = data.get('message_ids', [])
    message_ids.extend([msg.message_id, message.message_id])

    # Обновляем данные
    await state.update_data(message_ids=message_ids)

@router.message(UpdateProfile.real_name)
async def update_profile_real_name(message: Message, state: FSMContext):
    update_user(message.from_user.id, "real_name", message.text)
    await state.set_state(UpdateProfile.category)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Юноши", callback_data="update_category:Юноши")],
        [InlineKeyboardButton(text="Подростки", callback_data="update_category:Подростки")],
        [InlineKeyboardButton(text="Взрослые", callback_data="update_category:Взрослые")]
    ])
    await message.answer("Возрастная категория:", reply_markup=markup)


@router.callback_query(F.data.startswith("update_category:"))
async def update_profile_category(callback: CallbackQuery, state: FSMContext):
    _, category = callback.data.split(":")
    update_user(callback.from_user.id, "category", category)
    await callback.message.answer("Данные успешно обновлены!")
    await state.clear()


# === Запуск ===
async def backup_database():
    """Создание резервной копии базы данных"""
    try:
        backup_dir = "backups"
        os.makedirs(backup_dir, exist_ok=True)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{backup_dir}/backup_{timestamp}.sqlite"

        # Создаем копию базы
        with open(DB_PATH, 'rb') as source, open(backup_path, 'wb') as target:
            target.write(source.read())

        # Удаляем старые бэкапы (оставляем только последние 7)
        backup_files = sorted([f for f in os.listdir(backup_dir) if f.startswith("backup_")])
        for old_backup in backup_files[:-7]:
            os.remove(os.path.join(backup_dir, old_backup))

        logging.info(f"Database backup created: {backup_path}")
    except Exception as e:
        logging.error(f"Backup error: {e}")

async def scheduled_backup():
    """Запуск регулярного бэкапа"""
    while True:
        await backup_database()
        # Ждем 24 часа
        await asyncio.sleep(24 * 60 * 60)

async def main():
    # Проверка токена
    if not TOKEN:
        logging.error("No bot token provided! Please set BOT_TOKEN in Secrets")
        return

    # Запускаем задачу бэкапа
    asyncio.create_task(scheduled_backup())

    retry_count = 0
    max_retries = 5
    retry_delay = 5  # seconds

    while retry_count < max_retries:
        try:
            logging.info("Starting bot...")
            await dp.start_polling(bot, timeout=30)
            break
        except Exception as e:
            retry_count += 1
            logging.error(f"Error starting bot (attempt {retry_count}/{max_retries}): {e}")
            if retry_count < max_retries:
                logging.info(f"Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
            else:
                logging.error("Max retries reached. Please check your bot token and internet connection.")

def get_user_history(nickname: str) -> tuple[str, bool]:
    """Получает историю начислений пользователя"""
    logging.info(f"Getting history for user: {nickname}")
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # Проверяем существование пользователя
        cur.execute("SELECT * FROM users WHERE nickname = ?", (nickname,))
        user = cur.fetchone()
        if not user:
            logging.info(f"User {nickname} not found")
            return "История пуста", True

        # Получаем историю начислений
        cur.execute("""
            SELECT timestamp, points, note 
            FROM points_history 
            WHERE nickname = ? 
            ORDER BY timestamp DESC
        """, (nickname,))
        history = cur.fetchall()
        logging.info(f"Found {len(history)} history records for {nickname}")

        if not history:
            return "История пуста", True

        text = f"История начислений пользователя {nickname}:\n\n"
        for timestamp, points, note in history:
            text += f"({timestamp}) {points} баллов \"{note}\"\n"


        return text, False

    except sqlite3.Error as e:
        logging.error(f"Database error in get_user_history: {e}")
        return "Ошибка при получении истории", True
    except Exception as e:
        logging.error(f"Error in get_user_history: {e}")
        return "Произошла ошибка", True
    finally:
        if conn:
            conn.close()

@router.message(Command(commands=["история"]))
async def history_command(message: Message):
    """Обработчик команды /история <ник>"""
    try:
        logging.info(f"History command received: {message.text}")
        args = message.text.split()
        if len(args) < 2:
            await message.answer("Введите в формате /история <ник>")
            return

        nickname = args[1].strip()
        logging.info(f"History command called for user: {nickname}")

        # Проверяем существование пользователя
        if not get_user_by_nickname(nickname):
            await message.answer("Пользователь не найден")
            return

        history_text, is_empty = get_user_history(nickname)

        # Формируем кнопки для непустой истории
        markup = None
        if not is_empty:
            buttons = [[InlineKeyboardButton(text="« Назад к профилю", callback_data=f"back_to_profile:{nickname}")]]
            markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        # Отправляем ответ
        await message.answer(history_text, reply_markup=markup, parse_mode=ParseMode.HTML)
        logging.info(f"History response sent for {nickname}")

    except sqlite3.Error as e:
        logging.error(f"Database error in history command: {e}")
        await message.answer("Ошибка при получении истории")
    except Exception as e:
        logging.error(f"Error in history command: {e}")
        await message.answer("Произошла ошибка при получении истории")

@router.callback_query(F.data.startswith("history:"))
async def showpoints_history(callback: CallbackQuery):
    """Обработчик кнопки История начислений"""
    try:
        nickname = callback.data.split(":")[1]

        # Проверяем существование пользователя
        if not get_user_by_nickname(nickname):
            await callback.answer("Пользователь не найден")
            return

        history_text, is_empty = get_user_history(nickname)

        # Формируем кнопки
        buttons = [[InlineKeyboardButton(text="« Назад к профилю", callback_data=f"back_to_profile:{nickname}")]]
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        # Если есть фото в текущем сообщении, отправляем новое
        if callback.message.photo:
            await callback.message.answer(history_text, reply_markup=markup)
            await callback.message.delete()
        else:
            try:
                await callback.message.edit_text(history_text, reply_markup=markup, parse_mode=ParseMode.HTML)
            except Exception as edit_error:
                logging.error(f"Error editing message: {edit_error}")
                await callback.message.answer(history_text, reply_markup=markup)
                await callback.message.delete()

        await callback.answer()

    except sqlite3.Error as e:
        logging.error(f"Database error in show_points_history: {e}")
        await callback.answer("❌ Ошибка при работе с базой данных")
        logging.error(str(e))
    except Exception as e:
        logging.error(f"Unexpected error in show_points_history: {e}")
        await callback.answer("❌ Произошла непредвиденная ошибка")
        logging.error(str(e))

@router.callback_query(F.data.startswith("back_to_profile:"))
async def back_to_profile(callback: CallbackQuery):
    try:
        nickname = callback.data.split(":")[1]
        user = get_user_by_nickname(nickname)
        if not user:
            await callback.message.edit_text("Пользователь не найден")
            return

        # Получаем информацию о пользователе из Telegram
        try:
            chat = await bot.get_chat(user[0])
            username = f"@{chat.username}" if chat.username else "не указан"
        except Exception as e:
            logging.error(f"Error getting username: {e}")
            username = "не указан"

        # Форматируем дату регистрации
        try:
            registration_date = user[9].split('.')[0].replace('T', ' ') if user[9] else "Не указана"
        except (IndexError, AttributeError):
            registration_date = "Не указана"

        if callback.from_user.id in ADMIN_IDS:
            profile_text = f"Профиль пользователя {nickname}:\nИмя: {user[2]}\nКатегория: {user[4]}\nБаллы: {user[6]}\nУчастий: {user[7]}\n\nTelegram: {username}\nID: {user[0]}\nДата регистрации: {registration_date}"
        else:
            profile_text = f"Профиль пользователя {nickname}:\nИмя: {user[2]}\nКатегория: {user[4]}\nБаллы: {user[6]}\nУчастий: {user[7]}"

        buttons = [[InlineKeyboardButton(text="История начислений", callback_data=f"history:{user[1]}")]]
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        photo_path = user[8] if len(user) > 8 else None
        if photo_path and os.path.exists(photo_path):
            await callback.message.delete()
            await callback.message.answer_photo(photo=FSInputFile(photo_path), caption=profile_text, reply_markup=markup)
        else:
            await callback.message.edit_text(profile_text, reply_markup=markup)
        await callback.answer()
    except Exception as e:
        logging.error(f"Error in back_to_profile: {e}")
        await callback.message.edit_text("Произошла ошибка при возврате к профилю")

@router.message(Command(commands=["удалить_событие"]))
async def delete_event(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Используйте: /удалить_событие <название события>")
        return

    event_name = args[1]
    if event_name in EVENTS:
        del EVENTS[event_name]
        delete_event_db(event_name)
        await message.answer(f"Событие \"{event_name}\" удалено")
    else:
        await message.answer("Событие не найдено")

@router.message(Command(commands=["бэкап"]))
async def manual_backup(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await backup_database()
    await message.answer("Резервная копия базы данных создана")

async def get_or_create_invite_link(user_id: int, nickname: str) -> str:
    try:
        # Сначала проверяем, есть ли уже ссылка у пользователя
        cursor.execute("SELECT invite_link FROM user_invites WHERE user_id = ?", (user_id,))
        existing_link = cursor.fetchone()

        if existing_link:
            return existing_link[0]

        # Если ссылки нет, создаем новую
        CHAT_ID = -1002299467521  # ID чата для приглашений
        invite_link = await bot.create_chat_invite_link(
            chat_id=CHAT_ID,
            name=f"Invite by {nickname}",
            creates_join_request=False,
            member_limit=100,
            expire_date=None
        )

        # Сохраняем ссылку в БД
        cursor.execute("""
            INSERT INTO user_invites (user_id, invite_link) 
            VALUES (?, ?)
        """, (user_id, invite_link.invite_link))
        conn.commit()

        return invite_link.invite_link
    except Exception as e:
        logging.error(f"Error in get_or_create_invite_link: {e}")
        raise

@router.message(F.text == "Мое приглашение")
async def my_invite(message: Message):
    try:
        user = get_user(message.from_user.id)
        if not user:
            await message.answer("Вы не зарегистрированы.")
            return

        # Получаем или создаем ссылку-приглашение
        try:
            invite_link = await get_or_create_invite_link(message.from_user.id, user[1])
        except Exception as invite_error:
            logging.error(f"Error creating invite link: {invite_error}")
            await message.answer("Не удалось создать пригласительную ссылку. Попробуйте позже.")
            return

        invites_count = get_invites_count(message.from_user.id)

        # Создаем QR-код
        try:
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(invite_link)
            qr.make(fit=True)
            qr_image = qr.make_image(fill_color="black", back_color="white")

            # Сохраняем QR-код
            os.makedirs("qr_codes", exist_ok=True)
            qr_path = f"qr_codes/{message.from_user.id}_{int(datetime.datetime.now().timestamp())}.png"
            qr_image.save(qr_path)
        except Exception as qr_error:
            logging.error(f"Error generating QR code: {qr_error}")
            # Если не удалось создать QR, отправляем только ссылку
            await message.answer(
                f"Ваша уникальная пригласительная ссылка:\n{invite_link}\n\n"
                f"Мои приглашения: {invites_count}"
            )
            return

        try:
            # Отправляем сообщение с QR-кодом и ссылкой
            await message.answer_photo(
                FSInputFile(qr_path),
                caption=(
                    f"Ваша уникальная пригласительная ссылка:\n{invite_link}\n\n"
                    f"Мои приглашения: {invites_count}"
                )
            )
        except Exception as send_error:
            logging.error(f"Error sending message: {send_error}")
            await message.answer("Произошла ошибка при отправке приглашения.")
        finally:
            # Удаляем временный файл QR-кода
            try:
                if os.path.exists(qr_path):
                    os.remove(qr_path)
            except Exception as remove_error:
                logging.error(f"Error removing QR file: {remove_error}")

    except Exception as e:
        logging.error(f"Unexpected error in my_invite: {e}")
        await message.answer("Произошла непредвиденная ошибка. Попробуйте позже.")

@router.message()
async def unknown_command(message: Message):
    # Игнорируем сервисные сообщения
    if not message.text:
        return
    await message.answer("Неправильная команда. Используйте доступные команды:\n/start")

@router.callback_query(F.data == "cancel_update")
async def cancel_profile_update(callback: CallbackQuery, state: FSMContext):
    try:
        # Получаем и проверяем текущее состояние
        current_state = await state.get_state()
        if current_state is None:
            await callback.answer("Нет активного редактирования")
            return

        # Удаляем сообщения с вопросами
        data = await state.get_data()
        messages_to_delete = data.get('message_ids', [])
        if 'last_message_id' in data:
            messages_to_delete.append(data['last_message_id'])

        for msg_id in messages_to_delete:
            try:
                await bot.delete_message(callback.message.chat.id, msg_id)
            except Exception as e:
                logging.error(f"Error deleting message {msg_id}: {e}")

        # Очищаем состояние
        await state.clear()

        # Получаем актуальные данные пользователя
        user = get_user(callback.from_user.id)
        if not user:
            await callback.answer("Ошибка при получении данных профиля")
            return

        # Создаем стандартную клавиатуру
        buttons = [
            [InlineKeyboardButton(text="История начислений", callback_data=f"history:{user[1]}")],
            [InlineKeyboardButton(text="Обновить данные", callback_data="update_profile")]
        ]
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)

        # Формируем текст профиля
        profile_text = f"Ваш профиль:\nНикнейм: {user[1]}\nИмя: {user[2]}\nТелефон: {user[3]}\nКатегория: {user[4]}\nБаллы: {user[6]}\nУчастий: {user[7]}"

        # Обновляем или отправляем новое сообщение с профилем
        photo_path = user[8] if len(user) > 8 else None
        try:
            # Создаем стандартную клавиатуру для профиля
            buttons = [
                [InlineKeyboardButton(text="История начислений", callback_data=f"history:{user[1]}")],
                [InlineKeyboardButton(text="Обновить данные", callback_data="update_profile")]
            ]
            markup = InlineKeyboardMarkup(inline_keyboard=buttons)

            if photo_path and os.path.exists(photo_path):
                # Для сообщений с фото
                await callback.message.edit_caption(caption=profile_text, reply_markup=markup)
            else:
                # Для текстовых сообщений
                await callback.message.edit_text(profile_text, reply_markup=markup)

            await callback.answer("Редактирование отменено")
        except Exception as e:
            logging.error(f"Error updating profile message: {e}")
            # Если не удалось отредактировать, отправляем новое сообщение
            if photo_path and os.path.exists(photo_path):
                await callback.message.answer_photo(FSInputFile(photo_path), caption=profile_text, reply_markup=markup)
            else:
                await callback.message.answer(profile_text, reply_markup=markup)
            await callback.message.delete()

    except Exception as e:
        logging.error(f"Error in cancel_profile_update: {e}")
        await callback.answer("Редактирование прервано")
        await state.clear()

async def cleanup():
    """Закрытие соединений при выключении"""
    if conn:
        conn.close()
        logging.info("Database connection closed")

def get_invites_count(user_id):
    cursor.execute("SELECT COUNT(*) FROM users WHERE invited_by = ?", (user_id,))
    return cursor.fetchone()[0]

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
    except Exception as e:
        logging.error(f"Critical error: {e}")
    finally:
        asyncio.run(cleanup())