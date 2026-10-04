"""Telegram booking bot: pick a service, a day and a free time; get reminders; cancel."""

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from booking.db import Database, SlotTaken
from booking.schedule import (
    SERVICES, TZ, bookable_days, can_cancel, fmt_day, fmt_dt, fmt_time, free_slots,
)

log = logging.getLogger(__name__)

BTN_BOOK = "📅 Book"
BTN_MINE = "🗓 My bookings"
REMIND_AHEAD = timedelta(hours=2)


class ServiceCb(CallbackData, prefix="svc"):
    service: str


class DayCb(CallbackData, prefix="day"):
    service: str
    day: str  # ISO date


class SlotCb(CallbackData, prefix="slot"):
    service: str
    ts: int  # unix seconds


class CancelCb(CallbackData, prefix="cancel"):
    booking_id: int


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_BOOK), KeyboardButton(text=BTN_MINE)]],
                               resize_keyboard=True)


def services_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in SERVICES.values():
        kb.button(text=f"{s.name} · {s.minutes} min · {s.price:,} ₸".replace(",", " "),
                  callback_data=ServiceCb(service=s.id))
    kb.adjust(1)
    return kb.as_markup()


async def safe_edit(msg: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


def build_router(db: Database, admin_chat: int) -> Router:
    router = Router()

    async def notify_admin(bot: Bot, text: str) -> None:
        if admin_chat:
            try:
                await bot.send_message(admin_chat, text)
            except Exception:
                log.exception("Admin notification failed")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer("Hi! Book an appointment in a few taps.", reply_markup=main_menu())

    @router.message(F.text == BTN_BOOK)
    @router.message(Command("book"))
    async def book(message: Message) -> None:
        await message.answer("Choose a service:", reply_markup=services_kb())

    @router.callback_query(ServiceCb.filter())
    async def pick_service(call: CallbackQuery, callback_data: ServiceCb) -> None:
        service = SERVICES.get(callback_data.service)
        if not service:
            await call.answer("Service not available", show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        for d in bookable_days(now_utc()):
            kb.button(text=fmt_day(d), callback_data=DayCb(service=service.id, day=d.isoformat()))
        kb.adjust(3)
        await safe_edit(call.message, f"<b>{service.name}</b>\nChoose a day:", kb.as_markup())
        await call.answer()

    @router.callback_query(DayCb.filter())
    async def pick_day(call: CallbackQuery, callback_data: DayCb) -> None:
        service = SERVICES.get(callback_data.service)
        day = date.fromisoformat(callback_data.day)
        if not service:
            await call.answer("Service not available", show_alert=True)
            return
        slots = free_slots(day, service.minutes, await db.busy_on(day), now_utc())
        kb = InlineKeyboardBuilder()
        for t in slots:
            kb.button(text=fmt_time(t), callback_data=SlotCb(service=service.id, ts=int(t.timestamp())))
        kb.adjust(4)
        kb.row(InlineKeyboardButton(text="⬅️ Other day", callback_data=ServiceCb(service=service.id).pack()))
        text = (f"<b>{service.name}</b>, {fmt_day(day)}\nChoose a time:" if slots
                else f"<b>{service.name}</b>, {fmt_day(day)}\nNo free time left this day.")
        await safe_edit(call.message, text, kb.as_markup())
        await call.answer()

    @router.callback_query(SlotCb.filter())
    async def pick_slot(call: CallbackQuery, callback_data: SlotCb, bot: Bot) -> None:
        service = SERVICES.get(callback_data.service)
        start = datetime.fromtimestamp(callback_data.ts, TZ)
        # Re-check against the schedule: callback data comes from the client and must not be trusted.
        day = start.date()
        if not service or start not in free_slots(day, service.minutes, await db.busy_on(day), now_utc()):
            await call.answer("This time is no longer available", show_alert=True)
            return
        try:
            booking_id = await db.create(call.from_user.id, call.from_user.full_name, service.id,
                                         start, start + timedelta(minutes=service.minutes))
        except SlotTaken:
            await call.answer("Sorry, someone just took this time. Please pick another.", show_alert=True)
            return
        await safe_edit(call.message,
                        f"✅ Booked: <b>{service.name}</b>\n{fmt_dt(start)}\n\nWe'll remind you 2 hours before.")
        await call.answer("Booked!")
        await notify_admin(bot, f"🆕 Booking #{booking_id}\n{service.name} — {fmt_dt(start)}\n"
                                f"{escape(call.from_user.full_name)} (<a href=\"tg://user?id={call.from_user.id}\">chat</a>)")

    @router.message(F.text == BTN_MINE)
    @router.message(Command("my"))
    async def my_bookings(message: Message) -> None:
        bookings = await db.upcoming_for(message.from_user.id, now_utc())
        if not bookings:
            await message.answer("You have no upcoming bookings.")
            return
        kb = InlineKeyboardBuilder()
        lines = []
        for b in bookings:
            lines.append(f"• {SERVICES[b.service_id].name} — {fmt_dt(b.starts_at)}")
            kb.button(text=f"✖️ Cancel {fmt_dt(b.starts_at)}", callback_data=CancelCb(booking_id=b.id))
        kb.adjust(1)
        await message.answer("Your bookings:\n" + "\n".join(lines), reply_markup=kb.as_markup())

    @router.callback_query(CancelCb.filter())
    async def cancel(call: CallbackQuery, callback_data: CancelCb, bot: Bot) -> None:
        b = await db.get(callback_data.booking_id)
        if not b or b.user_id != call.from_user.id or b.status != "active":
            await call.answer("Booking not found", show_alert=True)
            return
        if not can_cancel(b.starts_at, now_utc()):
            await call.answer("Less than 2 hours left — please call us to cancel.", show_alert=True)
            return
        await db.cancel(b.id)
        await call.answer("Cancelled")
        await call.message.answer(f"Cancelled: {SERVICES[b.service_id].name}, {fmt_dt(b.starts_at)}")
        await notify_admin(bot, f"❌ Booking #{b.id} cancelled by the customer ({fmt_dt(b.starts_at)})")

    @router.message(Command("today"))
    async def today(message: Message) -> None:
        if not admin_chat or message.chat.id != admin_chat:
            return
        day = now_utc().astimezone(TZ).date()
        items = await db.day_schedule(day)
        if not items:
            await message.answer("No bookings today.")
            return
        await message.answer("📋 Today:\n" + "\n".join(
            f"{fmt_time(b.starts_at)}–{fmt_time(b.ends_at)} {SERVICES[b.service_id].name} — {escape(b.name)}"
            for b in items))

    return router


async def reminder_loop(bot: Bot, db: Database) -> None:
    while True:
        try:
            for b in await db.due_reminders(now_utc(), REMIND_AHEAD):
                try:
                    await bot.send_message(b.user_id, f"⏰ Reminder: {SERVICES[b.service_id].name} at "
                                                      f"{fmt_time(b.starts_at)} today. See you soon!")
                except Exception:
                    log.warning("Could not remind user %s", b.user_id)
                await db.mark_reminded(b.id)
        except Exception:
            log.exception("Reminder loop error")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(os.getenv("DB_PATH", "data/bookings.db"))
    await db.connect()
    bot = Bot(os.environ["BOT_TOKEN"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router(db, int(os.getenv("ADMIN_CHAT_ID", "0"))))
    reminders = asyncio.create_task(reminder_loop(bot, db))
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
