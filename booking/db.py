"""SQLite storage for bookings. Times are stored as UTC ISO strings."""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from booking.schedule import TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    name       TEXT    NOT NULL,
    service_id TEXT    NOT NULL,
    starts_at  TEXT    NOT NULL,
    ends_at    TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'active',
    reminded   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bookings_time ON bookings(status, starts_at);
"""


class SlotTaken(Exception):
    pass


@dataclass(frozen=True)
class Booking:
    id: int
    user_id: int
    name: str
    service_id: str
    starts_at: datetime
    ends_at: datetime
    status: str


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def _row(r) -> Booking:
    return Booking(r["id"], r["user_id"], r["name"], r["service_id"],
                   datetime.fromisoformat(r["starts_at"]), datetime.fromisoformat(r["ends_at"]), r["status"])


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        # One shared connection: serialise transactions so two coroutines never interleave inside one.
        self._tx_lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)  # explicit transactions below
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def busy_on(self, day: date) -> list[tuple[datetime, datetime]]:
        start = datetime.combine(day, datetime.min.time(), TZ)
        cur = await self.conn.execute(
            "SELECT starts_at, ends_at FROM bookings WHERE status = 'active' AND starts_at < ? AND ends_at > ?",
            (_iso(start + timedelta(days=1)), _iso(start)),
        )
        return [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in await cur.fetchall()]

    async def create(self, user_id: int, name: str, service_id: str, starts_at: datetime, ends_at: datetime) -> int:
        """Insert a booking unless it overlaps an active one. The check and insert are atomic."""
        async with self._tx_lock:
            return await self._create_locked(user_id, name, service_id, starts_at, ends_at)

    async def _create_locked(self, user_id, name, service_id, starts_at, ends_at) -> int:
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self.conn.execute(
                "SELECT 1 FROM bookings WHERE status = 'active' AND starts_at < ? AND ends_at > ? LIMIT 1",
                (_iso(ends_at), _iso(starts_at)),
            )
            if await cur.fetchone():
                raise SlotTaken()
            cur = await self.conn.execute(
                "INSERT INTO bookings (user_id, name, service_id, starts_at, ends_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, name, service_id, _iso(starts_at), _iso(ends_at)),
            )
            await self.conn.execute("COMMIT")
            return cur.lastrowid
        except BaseException:
            await self.conn.execute("ROLLBACK")
            raise

    async def get(self, booking_id: int) -> Booking | None:
        cur = await self.conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,))
        r = await cur.fetchone()
        return _row(r) if r else None

    async def upcoming_for(self, user_id: int, now: datetime) -> list[Booking]:
        cur = await self.conn.execute(
            "SELECT * FROM bookings WHERE user_id = ? AND status = 'active' AND starts_at > ? ORDER BY starts_at",
            (user_id, _iso(now)),
        )
        return [_row(r) for r in await cur.fetchall()]

    async def cancel(self, booking_id: int) -> None:
        async with self._tx_lock:
            await self.conn.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))

    async def day_schedule(self, day: date) -> list[Booking]:
        start = datetime.combine(day, datetime.min.time(), TZ)
        cur = await self.conn.execute(
            "SELECT * FROM bookings WHERE status = 'active' AND starts_at >= ? AND starts_at < ? ORDER BY starts_at",
            (_iso(start), _iso(start + timedelta(days=1))),
        )
        return [_row(r) for r in await cur.fetchall()]

    async def due_reminders(self, now: datetime, ahead: timedelta) -> list[Booking]:
        cur = await self.conn.execute(
            "SELECT * FROM bookings WHERE status = 'active' AND reminded = 0 AND starts_at > ? AND starts_at <= ?",
            (_iso(now), _iso(now + ahead)),
        )
        return [_row(r) for r in await cur.fetchall()]

    async def mark_reminded(self, booking_id: int) -> None:
        async with self._tx_lock:
            await self.conn.execute("UPDATE bookings SET reminded = 1 WHERE id = ?", (booking_id,))
