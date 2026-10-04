"""Services, working hours and free-slot calculation. Pure logic, no Telegram or DB."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Almaty")


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    minutes: int
    price: int  # tenge


SERVICES: dict[str, Service] = {s.id: s for s in [
    Service("haircut", "Haircut", 60, 7000),
    Service("manicure", "Manicure + gel polish", 90, 7000),
    Service("brows", "Brow shaping & tint", 30, 4500),
    Service("coloring", "Hair colouring", 150, 18000),
]}


@dataclass(frozen=True)
class WorkingHours:
    opens: time = time(10, 0)
    closes: time = time(20, 0)
    step_minutes: int = 30
    days_off: frozenset[int] = field(default_factory=lambda: frozenset({6}))  # 0=Mon … 6=Sun
    min_lead_minutes: int = 60  # can't book a slot starting sooner than this


HOURS = WorkingHours()


def bookable_days(now: datetime, count: int = 7, hours: WorkingHours = HOURS) -> list[date]:
    """Next `count` working days starting today (local time)."""
    day = now.astimezone(TZ).date()
    days: list[date] = []
    while len(days) < count:
        if day.weekday() not in hours.days_off:
            days.append(day)
        day += timedelta(days=1)
    return days


def free_slots(
    day: date,
    minutes: int,
    busy: list[tuple[datetime, datetime]],
    now: datetime,
    hours: WorkingHours = HOURS,
) -> list[datetime]:
    """Start times (aware, local) on `day` where a `minutes`-long service fits without overlapping `busy`."""
    if day.weekday() in hours.days_off:
        return []
    start = datetime.combine(day, hours.opens, TZ)
    close = datetime.combine(day, hours.closes, TZ)
    earliest = now + timedelta(minutes=hours.min_lead_minutes)
    length = timedelta(minutes=minutes)
    slots = []
    t = start
    while t + length <= close:
        end = t + length
        if t >= earliest and not any(t < b_end and b_start < end for b_start, b_end in busy):
            slots.append(t)
        t += timedelta(minutes=hours.step_minutes)
    return slots


def can_cancel(start: datetime, now: datetime, min_hours: int = 2) -> bool:
    return start - now >= timedelta(hours=min_hours)


def fmt_day(d: date) -> str:
    return d.strftime("%a %d.%m")


def fmt_time(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%H:%M")


def fmt_dt(t: datetime) -> str:
    return t.astimezone(TZ).strftime("%a %d.%m %H:%M")
