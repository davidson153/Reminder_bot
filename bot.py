"""
Telegram Reminder Bot

Бот для установки напоминаний с возможностью:
- добавления нового напоминания,-
- удаления напоминания,-
- отложки напоминания на 10 минут.

Технологии:
- aiogram 3.x
- apscheduler
- хранение данных в JSON

Автор: David
"""
import asyncio
import json
import re
import uuid
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.exceptions import TelegramForbiddenError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.base import JobLookupError
from dotenv import load_dotenv
load_dotenv()

#НАСТРОЙКИ
API_TOKEN = os.getenv("BOT_TOKEN")

REMINDERS_FILE = "reminders.json"

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

reminders = []  # локальное хранилище


#FSM
class ReminderForm(StatesGroup):
    waiting_for_time = State()
    waiting_for_text = State()


#Хранилище
def load_reminders():
    global reminders
    try:
        with open(REMINDERS_FILE, "r") as f:
            reminders = json.load(f)

        changed = False
        for r in reminders:
            if "job_id" not in r:
                r["job_id"] = f"{r['chat_id']}_{uuid.uuid4()}"
                changed = True
        if changed:
            save_reminders(reminders)

    except (FileNotFoundError, json.JSONDecodeError):
        reminders = []


def save_reminders(data):
    with open(REMINDERS_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


async def safe_send_message(chat_id, text, **kwargs):
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        global reminders
        reminders = [r for r in reminders if r["chat_id"] != chat_id]
        save_reminders(reminders)
        print(f"❌ Пользователь {chat_id} заблокировал бота. Напоминания удалены.")


#Кнопки
def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить напоминание", callback_data="add_reminder")],
            [InlineKeyboardButton(text="📋 Мои напоминания", callback_data="list_reminders")]
        ]
    )


def reminder_actions(job_id: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏰ Отложить на 10 мин", callback_data=f"delay_{job_id}")],
            [InlineKeyboardButton(text="❌ Удалить", callback_data=f"delete_{job_id}")]
        ]
    )


def reminders_menu(user_reminders):
    keyboard = []
    for idx, r in enumerate(user_reminders, 1):
        run_time = datetime.fromisoformat(r["time"]).strftime("%H:%M")
        keyboard.append([
            InlineKeyboardButton(
                text=f"❌ {idx}. {run_time} — {r['text'][:20]}",
                callback_data=f"delete_{r['job_id']}"
            )
        ])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


#Парсинг времени
def normalize_time(raw: str) -> str:
    if not raw:
        raise ValueError("empty")

    s = raw.strip()
    s = s.replace("\uFF1A", ":").replace("\u2236", ":")
    s = re.sub(r"[.\-–—\s]+", ":", s)
    s = re.sub(r"[^0-9:]", "", s)

    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", s)
    if m:
        h, m_ = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= m_ <= 59:
            return f"{h:02d}:{m_:02d}"
        raise ValueError("range")

    if re.fullmatch(r"\d{3,4}", s):
        if len(s) == 3:
            h, m_ = int(s[0]), int(s[1:])
        else:
            h, m_ = int(s[:2]), int(s[2:])
        if 0 <= h <= 23 and 0 <= m_ <= 59:
            return f"{h:02d}:{m_:02d}"
        raise ValueError("range")

    raise ValueError("pattern")


#Приветствие
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот-напоминалка.\n\n"
        "Нажми кнопку, чтобы добавить или посмотреть напоминания:",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data == "add_reminder")
async def add_reminder_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("⏰ Введи время напоминания (например 9:30, 09.30, 930):")
    await state.set_state(ReminderForm.waiting_for_time)


@dp.message(ReminderForm.waiting_for_time)
async def process_time(message: types.Message, state: FSMContext):
    try:
        norm = normalize_time(message.text)
        await state.update_data(time=norm)
        await message.answer("📝 Теперь введи текст напоминания:")
        await state.set_state(ReminderForm.waiting_for_text)
    except ValueError:
        await message.answer("❌ Формат неверный. Введи время в формате HH:MM (например 09:30).")


@dp.message(ReminderForm.waiting_for_text)
async def process_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    reminder_time = datetime.strptime(data["time"], "%H:%M").time()

    now = datetime.now()
    reminder_datetime = datetime.combine(now.date(), reminder_time)
    if reminder_datetime < now:
        reminder_datetime += timedelta(days=1)

    job_id = f"{message.chat.id}_{uuid.uuid4()}"

    reminder = {
        "chat_id": message.chat.id,
        "time": reminder_datetime.isoformat(),
        "text": message.text,
        "job_id": job_id
    }
    reminders.append(reminder)
    save_reminders(reminders)

    scheduler.add_job(
        send_reminder,
        "date",
        run_date=reminder_datetime,
        args=[message.chat.id, message.text, job_id],
        id=job_id,
        misfire_grace_time=30
    )

    await message.answer(
        f"✅ Напоминание установлено на {reminder_time.strftime('%H:%M')}: {message.text}",
        reply_markup=main_menu()
    )
    await state.clear()


@dp.callback_query(F.data == "list_reminders")
async def list_reminders(callback: CallbackQuery):
    user_reminders = [r for r in reminders if r["chat_id"] == callback.message.chat.id]
    if not user_reminders:
        await callback.message.edit_text("📭 У тебя нет активных напоминаний.", reply_markup=main_menu())
        return

    text = "📋 Твои напоминания:\n\n"
    for idx, r in enumerate(user_reminders, 1):
        run_time = datetime.fromisoformat(r["time"]).strftime("%H:%M")
        text += f"{idx}. {run_time} — {r['text']}\n"

    await callback.message.edit_text(text, reply_markup=reminders_menu(user_reminders))
    await callback.answer()


@dp.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    await callback.message.edit_text("⬅️ Главное меню", reply_markup=main_menu())


#Напоминания
async def send_reminder(chat_id: int, text: str, job_id: str):
    await safe_send_message(
        chat_id,
        f"🔔 Напоминание: {text}",
        reply_markup=reminder_actions(job_id)
    )


@dp.callback_query(F.data.startswith("delete_"))
async def process_delete_callback(callback: CallbackQuery):
    job_id = callback.data.split("_", 1)[1]
    reminder_to_delete = next((r for r in reminders if r["job_id"] == job_id), None)

    if not reminder_to_delete:
        await callback.message.answer("❌ Напоминание не найдено.")
        return

    reminders.remove(reminder_to_delete)
    save_reminders(reminders)

    try:
        scheduler.remove_job(job_id)
    except JobLookupError:
        pass

    await callback.message.edit_text("❌ Напоминание удалено.", reply_markup=main_menu())
    await callback.answer()


@dp.callback_query(F.data.startswith("delay_"))
async def process_delay_callback(callback: CallbackQuery):
    job_id = callback.data.split("_", 1)[1]
    reminder = next((r for r in reminders if r["job_id"] == job_id), None)

    if not reminder:
        await callback.message.answer("❌ Напоминание не найдено.")
        return

    new_time = datetime.fromisoformat(reminder["time"]) + timedelta(minutes=10)
    reminder["time"] = new_time.isoformat()
    save_reminders(reminders)

    scheduler.add_job(
        send_reminder,
        "date",
        run_date=new_time,
        args=[callback.message.chat.id, reminder["text"], job_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=30
    )

    await callback.message.edit_text(
        f"⏰ Напоминание отложено на 10 минут: {reminder['text']}",
        reply_markup=main_menu()
    )
    await callback.answer()


#MAIN
async def main():
    load_reminders()
    scheduler.start()

    for r in reminders:
        run_time = datetime.fromisoformat(r["time"])
        if run_time > datetime.now():
            scheduler.add_job(
                send_reminder,
                "date",
                run_date=run_time,
                args=[r["chat_id"], r["text"], r["job_id"]],
                id=r["job_id"],
                misfire_grace_time=30
            )

    print("✅ Бот запущен")  # вывод в консоль
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
