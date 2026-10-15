import os
import asyncio
import logging
import sqlite3
from datetime import datetime
import aiohttp

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery,
    URLInputFile,
)

# =====================================================================
# ⚙️ НАСТРОЙКИ БОТА
# Вы можете задать их через Переменные окружения (Environment Variables)
# в панели Bothost («Дополнительные настройки») ИЛИ вписать прямо сюда:
# =====================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН_ОТ_BOTFATHER")
SUNO_API_KEY = os.getenv("SUNO_API_KEY", "ВСТАВЬ_КЛЮЧ_ОТ_SUNOAPI_ORG")
SUNO_BASE_URL = os.getenv("SUNO_BASE_URL", "https://api.sunoapi.org").rstrip("/")

# Ваш Telegram ID (можно узнать в @userinfobot), чтобы работала команда /admin
raw_admins = os.getenv("ADMIN_IDS", "123456789")
ADMIN_IDS = [int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()]

# Экономика бота
WELCOME_FREE_GENERATIONS = int(os.getenv("WELCOME_FREE_GENERATIONS", "1"))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "V6")

# Тарифы пополнения в Telegram Stars (Звездах)
TARIFFS = {
    "pack_1": (1, 25, "🎵 1 генерация (2 трека) — 25 ⭐"),
    "pack_5": (5, 100, "🔥 5 генераций (10 треков) — 100 ⭐"),
    "pack_15": (15, 250, "💎 15 генераций (30 треков) — 250 ⭐"),
}

# =====================================================================
# 🗄 БАЗА ДАННЫХ (SQLite)
# =====================================================================
DB_NAME = "suno_commercial.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                total_generated INTEGER DEFAULT 0,
                joined_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                user_id INTEGER,
                title TEXT,
                status TEXT,
                created_at TEXT
            )
        """)
        conn.commit()

def get_or_create_user(user_id: int, username: str = ""):
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute("SELECT balance, total_generated FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            now = datetime.now().strftime("%Y-%m-%d %H-%M")
            c.execute(
                "INSERT INTO users (user_id, username, balance, total_generated, joined_at) VALUES (?, ?, ?, 0, ?)",
                (user_id, username or "", WELCOME_FREE_GENERATIONS, now)
            )
            conn.commit()
            return WELCOME_FREE_GENERATIONS, 0
        return row[0], row[1]

def update_balance(user_id: int, delta: int, increment_generated: bool = False):
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        if increment_generated:
            c.execute(
                "UPDATE users SET balance = balance + ?, total_generated = total_generated + 1 WHERE user_id = ?",
                (delta, user_id)
            )
        else:
            c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (delta, user_id))
        conn.commit()

def get_stats():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*), SUM(total_generated), SUM(balance) FROM users")
        return c.fetchone()

# =====================================================================
# 🎹 СОСТОЯНИЯ ДИАЛОГА (FSM)
# =====================================================================
class GenStates(StatesGroup):
    simple_prompt = State()
    custom_title = State()
    custom_style = State()
    custom_lyrics = State()

# =====================================================================
# 🎛 КЛАВИАТУРЫ
# =====================================================================
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Создать музыку (Быстрый режим)", callback_data="start_simple")],
        [InlineKeyboardButton(text="🎛 Профи-режим (Custom Mode)", callback_data="start_custom")],
        [
            InlineKeyboardButton(text="💳 Купить генерации", callback_data="buy_menu"),
            InlineKeyboardButton(text="👤 Мой профиль", callback_data="profile")
        ]
    ])

def instrumental_kb(prefix: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎤 С вокалом", callback_data=f"{prefix}_vocal"),
            InlineKeyboardButton(text="🎹 Инструментал (Без слов)", callback_data=f"{prefix}_inst")
        ],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel")]
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel")]
    ])

def buy_kb():
    buttons = []
    for pack_id, (gens, stars, label) in TARIFFS.items():
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"pay_{pack_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# =====================================================================
# 🌐 РАБОТА С SUNO API
# =====================================================================
async def suno_create_task(payload: dict) -> tuple[bool, str]:
    headers = {
        "Authorization": f"Bearer {SUNO_API_KEY}",
        "Content-Type": "application/json"
    }
    payload["callBackUrl"] = "https://example.com/callback"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{SUNO_BASE_URL}/api/v1/generate", json=payload, headers=headers, timeout=30) as resp:
                data = await resp.json()
                if data.get("code") == 200 and data.get("data", {}).get("taskId"):
                    return True, data["data"]["taskId"]
                return False, data.get("msg", f"Код ошибки: {data.get('code')}")
    except Exception as e:
        return False, str(e)

async def suno_get_credits() -> str:
    headers = {"Authorization": f"Bearer {SUNO_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{SUNO_BASE_URL}/api/v1/generate/credit", headers=headers, timeout=15) as resp:
                data = await resp.json()
                if data.get("code") == 200:
                    return str(data.get("data"))
                return f"Ошибка: {data.get('msg')}"
    except Exception as e:
        return f"Недоступно ({e})"

async def poll_and_deliver(bot: Bot, user_id: int, task_id: str, title_hint: str, status_msg_id: int):
    headers = {"Authorization": f"Bearer {SUNO_API_KEY}"}
    max_attempts = 40

    async with aiohttp.ClientSession() as session:
        for _ in range(max_attempts):
            await asyncio.sleep(12)
            try:
                url = f"{SUNO_BASE_URL}/api/v1/generate/record-info?taskId={task_id}"
                async with session.get(url, headers=headers, timeout=20) as resp:
                    res = await resp.json()
                    if res.get("code") != 200 or not res.get("data"):
                        continue

                    d = res["data"]
                    status = d.get("status", "PENDING")

                    if status == "SUCCESS":
                        tracks = d.get("response", {}).get("sunoData") or d.get("response", {}).get("data") or []
                        try:
                            await bot.delete_message(chat_id=user_id, message_id=status_msg_id)
                        except Exception:
                            pass

                        await bot.send_message(
                            user_id,
                            f"✅ <b>Ваша музыка готова!</b>\nЗадача: <code>{task_id}</code>",
                            parse_mode="HTML"
                        )

                        for idx, tr in enumerate(tracks, start=1):
                            audio_url = tr.get("audio_url") or tr.get("audioUrl")
                            title = tr.get("title") or title_hint or f"Track {idx}"
                            tags = tr.get("tags") or ""
                            image_url = tr.get("image_url") or tr.get("imageUrl")

                            caption = f"🎵 <b>{title}</b> (Вариант {idx})\n🏷 Стиль: <i>{tags}</i>"
                            if audio_url:
                                try:
                                    audio_file = URLInputFile(audio_url, filename=f"{title}_{idx}.mp3")
                                    thumb = URLInputFile(image_url) if image_url else None
                                    await bot.send_audio(
                                        chat_id=user_id,
                                        audio=audio_file,
                                        title=title,
                                        performer="AI Music Studio",
                                        caption=caption,
                                        thumbnail=thumb,
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    await bot.send_message(
                                        user_id,
                                        f"{caption}\n\n📥 <a href='{audio_url}'>Скачать MP3 напрямую</a>",
                                        parse_mode="HTML"
                                    )

                        await bot.send_message(user_id, "Хотите создать ещё один трек?", reply_markup=main_menu_kb())
                        return

                    elif status in ("FAILED", "CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED", "SENSITIVE_WORD_ERROR"):
                        err_msg = d.get("errorMessage") or status
                        update_balance(user_id, 1)
                        await bot.edit_message_text(
                            chat_id=user_id,
                            message_id=status_msg_id,
                            text=f"❌ <b>Генерация не удалась</b> ({err_msg}).\n\n"
                                 f"💡 Списанная генерация <b>возвращена на ваш баланс</b>.",
                            parse_mode="HTML",
                            reply_markup=main_menu_kb()
                        )
                        return

            except Exception as e:
                logging.error(f"Ошибка опроса задачи {task_id}: {e}")

        update_balance(user_id, 1)
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=status_msg_id,
            text="⌛ Превышено время ожидания ответа от сервера. Генерация возвращена на ваш баланс.",
            reply_markup=main_menu_kb()
        )

# =====================================================================
# 🤖 ОБРАБОТЧИКИ КОМАНД И КНОПОК
# =====================================================================
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    balance, _ = get_or_create_user(message.from_user.id, message.from_user.username)
    text = (
        f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
        f"Я — студия создания музыки на базе нейросети <b>Suno {DEFAULT_MODEL}</b>.\n"
        f"За 1 генерацию нейросеть создает сразу <b>2 полноценных трека</b> в формате MP3.\n\n"
        f"🎫 Ваш баланс: <b>{balance} ген.</b>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb())

@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    balance, _ = get_or_create_user(call.from_user.id, call.from_user.username)
    await call.message.edit_text(
        f"Главное меню.\n🎫 Ваш баланс: <b>{balance} ген.</b>",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )

@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    balance, total = get_or_create_user(call.from_user.id, call.from_user.username)
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{call.from_user.id}</code>\n"
        f"🎫 Доступно генераций: <b>{balance}</b>\n"
        f"🎵 Всего создано: <b>{total * 2} треков</b>"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=buy_kb())

@router.callback_query(F.data == "start_simple")
async def cb_start_simple(call: CallbackQuery, state: FSMContext):
    balance, _ = get_or_create_user(call.from_user.id)
    if balance <= 0:
        await call.message.edit_text(
            "⚠️ На вашем балансе закончились генерации.\nПополните баланс, чтобы продолжить:",
            reply_markup=buy_kb()
        )
        return
    await call.message.edit_text(
        "✨ <b>Быстрый режим</b>\nВыберите формат будущего трека:",
        parse_mode="HTML",
        reply_markup=instrumental_kb("simple")
    )

@router.callback_query(F.data.startswith("simple_"))
async def cb_simple_type(call: CallbackQuery, state: FSMContext):
    is_inst = call.data.endswith("_inst")
    await state.update_data(instrumental=is_inst)
    await state.set_state(GenStates.simple_prompt)
    await call.message.edit_text(
        "📝 <b>Опишите идею трека:</b>\n"
        "Укажите жанр, настроение, инструменты или тему песни (до 2500 символов):",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )

@router.message(GenStates.simple_prompt)
async def process_simple_prompt(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()

    balance, _ = get_or_create_user(message.from_user.id)
    if balance <= 0:
        await message.answer("⚠️ Недостаточно генераций на балансе.", reply_markup=buy_kb())
        return

    prompt = message.text.strip()[:2500]
    payload = {
        "customMode": False,
        "instrumental": data.get("instrumental", False),
        "model": DEFAULT_MODEL,
        "prompt": prompt,
        "style": prompt[:200]
    }

    await launch_generation(message, bot, payload, prompt[:40])

@router.callback_query(F.data == "start_custom")
async def cb_start_custom(call: CallbackQuery):
    balance, _ = get_or_create_user(call.from_user.id)
    if balance <= 0:
        await call.message.edit_text(
            "⚠️ На вашем балансе закончились генерации.\nПополните баланс, чтобы продолжить:",
            reply_markup=buy_kb()
        )
        return
    await call.message.edit_text(
        "🎛 <b>Профи-режим (Custom Mode)</b>\nВыберите тип трека:",
        parse_mode="HTML",
        reply_markup=instrumental_kb("custom")
    )

@router.callback_query(F.data.startswith("custom_"))
async def cb_custom_type(call: CallbackQuery, state: FSMContext):
    is_inst = call.data.endswith("_inst")
    await state.update_data(instrumental=is_inst)
    await state.set_state(GenStates.custom_title)
    await call.message.edit_text(
        "1️⃣ <b>Введите название трека (Title):</b>\n<i>(например: Silent Cosmos)</i>",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )

@router.message(GenStates.custom_title)
async def process_custom_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip()[:80])
    await state.set_state(GenStates.custom_style)
    await message.answer(
        "2️⃣ <b>Введите стиль, жанр и инструменты (Style):</b>\n"
        "<i>Например: Deep ambient, shamanic flute, warm analog synth pads, slow meditative 60 bpm</i>",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )

@router.message(GenStates.custom_style)
async def process_custom_style(message: Message, state: FSMContext, bot: Bot):
    style = message.text.strip()[:1000]
    await state.update_data(style=style)
    data = await state.get_data()

    if data.get("instrumental"):
        await state.clear()
        payload = {
            "customMode": True,
            "instrumental": True,
            "model": DEFAULT_MODEL,
            "title": data.get("title"),
            "style": style,
            "prompt": style
        }
        await launch_generation(message, bot, payload, data.get("title", "Instrumental"))
    else:
        await state.set_state(GenStates.custom_lyrics)
        await message.answer(
            "3️⃣ <b>Отправьте текст песни (Lyrics) со структурными тегами:</b>\n"
            "<i>Например:\n[Verse]\nТвои слова...\n[Chorus]\nПрипев...</i>",
            parse_mode="HTML",
            reply_markup=cancel_kb()
        )

@router.message(GenStates.custom_lyrics)
async def process_custom_lyrics(message: Message, state: FSMContext, bot: Bot):
    lyrics = message.text.strip()[:4500]
    data = await state.get_data()
    await state.clear()

    payload = {
        "customMode": True,
        "instrumental": False,
        "model": DEFAULT_MODEL,
        "title": data.get("title"),
        "style": data.get("style"),
        "lyrics": lyrics,
        "prompt": lyrics
    }
    await launch_generation(message, bot, payload, data.get("title", "Custom Song"))

async def launch_generation(message: Message, bot: Bot, payload: dict, title_hint: str):
    user_id = message.from_user.id
    balance, _ = get_or_create_user(user_id)
    if balance <= 0:
        await message.answer("⚠️ Недостаточно генераций на балансе.", reply_markup=buy_kb())
        return

    update_balance(user_id, -1, increment_generated=True)
    status_msg = await message.answer("⏳ <b>Отправляю задачу в Suno AI...</b>", parse_mode="HTML")

    ok, result = await suno_create_task(payload)
    if not ok:
        update_balance(user_id, 1)
        await status_msg.edit_text(
            f"❌ <b>Ошибка запуска генерации:</b> {result}\n"
            f"Генерация возвращена на ваш баланс.",
            parse_mode="HTML",
            reply_markup=main_menu_kb()
        )
        return

    task_id = result
    await status_msg.edit_text(
        f"🎶 <b>Музыка создается!</b>\n"
        f"ID задачи: <code>{task_id}</code>\n\n"
        f"Обычно это занимает 1–2 минуты. Как только треки будут готовы, я сразу пришлю MP3-файлы сюда в чат!",
        parse_mode="HTML"
    )

    asyncio.create_task(poll_and_deliver(bot, user_id, task_id, title_hint, status_msg.message_id))

@router.callback_query(F.data == "buy_menu")
async def cb_buy_menu(call: CallbackQuery):
    await call.message.edit_text(
        "💎 <b>Пополнение баланса</b>\n\n"
        "1 генерация = <b>2 полноценных MP3-трека</b>.\n"
        "Выберите подходящий пакет:",
        parse_mode="HTML",
        reply_markup=buy_kb()
    )

@router.callback_query(F.data.startswith("pay_"))
async def cb_send_invoice(call: CallbackQuery, bot: Bot):
    pack_id = call.data.replace("pay_", "")
    if pack_id not in TARIFFS:
        return
    gens, stars, label = TARIFFS[pack_id]

    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"Пакет: {gens} генераций музыки",
        description=f"Пополнение баланса на {gens} генераций ({gens * 2} треков) в Suno AI Studio.",
        payload=f"topup:{gens}",
        currency="XTR",
        prices=[LabeledPrice(label=label, amount=stars)]
    )
    await call.answer()

@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("topup:"):
        gens = int(payload.split(":")[1])
        update_balance(message.from_user.id, gens)
        new_bal, _ = get_or_create_user(message.from_user.id)
        await message.answer(
            f"🎉 <b>Оплата прошла успешно!</b>\n"
            f"Начислено: <b>+{gens} ген.</b>\n"
            f"Ваш текущий баланс: <b>{new_bal} ген.</b>",
            parse_mode="HTML",
            reply_markup=main_menu_kb()
        )

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    users_count, total_gen, total_bal = get_stats()
    suno_credits = await suno_get_credits()
    text = (
        f"🔐 <b>Панель администратора</b>\n\n"
        f"👥 Всего пользователей: <b>{users_count or 0}</b>\n"
        f"🎵 Выполнено генераций: <b>{total_gen or 0}</b>\n"
        f"🎫 На руках у клиентов: <b>{total_bal or 0} ген.</b>\n"
        f"🔋 Остаток кредитов на sunoapi.org: <b>{suno_credits}</b>\n\n"
        f"Чтобы начислить генерации вручную:\n"
        f"<code>/give ID_пользователя количество</code>"
    )
    await message.answer(text, parse_mode="HTML")

@router.message(Command("give"))
async def cmd_give(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].lstrip("-").isdigit():
        await message.answer("Формат: <code>/give 123456789 5</code>", parse_mode="HTML")
        return
    target_id = int(parts[1])
    amount = int(parts[2])
    get_or_create_user(target_id)
    update_balance(target_id, amount)
    await message.answer(f"✅ Пользователю <code>{target_id}</code> начислено <b>{amount}</b> генераций.", parse_mode="HTML")

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    print("🚀 Коммерческий Suno-бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
