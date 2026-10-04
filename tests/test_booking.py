import asyncio
from datetime import date, datetime, timedelta

import pytest

from booking.db import Database, SlotTaken
from booking.schedule import TZ, WorkingHours, bookable_days, can_cancel, free_slots

MONDAY = date(2026, 10, 5)
EARLY = datetime(2026, 10, 1, 9, 0, tzinfo=TZ)  # well before Monday


def at(h, m=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def test_full_free_day():
    slots = free_slots(MONDAY, 60, [], EARLY)
    assert slots[0] == at(10) and slots[-1] == at(19)  # 19:00 + 60 min ends at closing
    assert len(slots) == 19  # every 30 minutes


def test_busy_time_is_excluded_with_service_length():
    busy = [(at(12), at(13))]
    slots = free_slots(MONDAY, 90, busy, EARLY)
    assert at(10, 30) in slots           # 10:30–12:00 fits exactly
    assert at(11) not in slots           # 11:00–12:30 overlaps
    assert at(12, 30) not in slots
    assert at(13) in slots


def test_lead_time_and_past_slots():
    now = at(14, 10)
    slots = free_slots(MONDAY, 30, [], now)
    assert slots[0] == at(15, 30)  # 14:10 + 60 min lead → first slot at 15:30


def test_day_off():
    sunday = date(2026, 10, 11)
    assert free_slots(sunday, 30, [], EARLY) == []
    assert sunday not in bookable_days(EARLY)
    assert len(bookable_days(EARLY, count=7)) == 7


def test_custom_hours():
    hours = WorkingHours(step_minutes=60, days_off=frozenset())
    assert len(free_slots(MONDAY, 60, [], EARLY, hours)) == 10


def test_can_cancel():
    assert can_cancel(at(15), at(12))
    assert not can_cancel(at(15), at(13, 30))


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "b.db"))
    await d.connect()
    yield d
    await d.close()


async def test_create_and_overlap(db):
    await db.create(1, "Aru", "haircut", at(12), at(13))
    with pytest.raises(SlotTaken):
        await db.create(2, "Dana", "brows", at(12, 30), at(13))
    await db.create(2, "Dana", "brows", at(13), at(13, 30))  # back-to-back is fine
    assert len(await db.busy_on(MONDAY)) == 2


async def test_concurrent_requests_for_same_slot(db):
    results = await asyncio.gather(
        *[db.create(i, f"u{i}", "haircut", at(15), at(16)) for i in range(5)], return_exceptions=True
    )
    assert sum(isinstance(r, int) for r in results) == 1
    assert sum(isinstance(r, SlotTaken) for r in results) == 4


async def test_cancel_frees_slot_and_listing(db):
    bid = await db.create(1, "Aru", "haircut", at(12), at(13))
    assert [b.id for b in await db.upcoming_for(1, EARLY)] == [bid]
    await db.cancel(bid)
    assert await db.upcoming_for(1, EARLY) == []
    await db.create(2, "Dana", "haircut", at(12), at(13))  # slot is free again


async def test_reminders(db):
    bid = await db.create(1, "Aru", "haircut", at(12), at(13))
    assert await db.due_reminders(at(9), timedelta(hours=2)) == []
    due = await db.due_reminders(at(10, 30), timedelta(hours=2))
    assert [b.id for b in due] == [bid]
    await db.mark_reminded(bid)
    assert await db.due_reminders(at(10, 30), timedelta(hours=2)) == []


async def test_day_schedule(db):
    await db.create(1, "Aru", "haircut", at(15), at(16))
    await db.create(2, "Dana", "brows", at(10), at(10, 30))
    await db.create(3, "Next day", "brows", at(10, day=MONDAY + timedelta(days=1)),
                    at(10, 30, day=MONDAY + timedelta(days=1)))
    assert [b.name for b in await db.day_schedule(MONDAY)] == ["Dana", "Aru"]
