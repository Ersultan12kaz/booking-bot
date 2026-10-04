# Telegram Appointment Booking Bot

Customers book a salon (or clinic, barber, tutor…) appointment in a few taps: **service → day →
free time → booked**. They get a reminder two hours before and can cancel from the chat. Staff get
every booking and cancellation in their own chat and can see today's schedule.

Built with **Python 3.13, aiogram 3 and SQLite**.

## Features

- Services with duration and price; working hours, slot step, days off and booking lead time are
  configurable (`booking/schedule.py`)
- **Free slots are calculated from real bookings and service length** — a 90-minute service only
  shows times where 90 minutes are actually free
- **No double booking:** the overlap check and insert run in one transaction under a lock; a test
  fires 5 simultaneous requests for one slot and exactly one wins
- Callback data from Telegram is re-validated on the server, so a forged button can't book outside
  working hours
- Reminders 2 hours before (background task), cancellation allowed until 2 hours before
- Staff chat: new booking / cancellation alerts with a link to the customer, `/today` schedule
- Times stored in UTC, shown in Almaty time

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export BOT_TOKEN=...          # from @BotFather
export ADMIN_CHAT_ID=...      # staff chat id
.venv/bin/python -m booking.bot
```

## Tests

```bash
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest
```

11 tests cover slot maths (overlaps, service length, lead time, days off), concurrency, cancel,
reminders and the daily schedule.
