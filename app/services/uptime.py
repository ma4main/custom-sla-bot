"""Доклад о простое после возвращения.

Воркер пишет пульс в базу в КОНЦЕ тика (отметка доказывает работу, а не таймер);
на старте разрыв с последним пульсом больше порога — одно сообщение владельцу.
Первый запуск (пульса нет) простоем не считается.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting
from app.services.transcript import fmt_duration

STATE_KEY = "uptime_runtime"
GAP_THRESHOLD = timedelta(minutes=5)


async def load_last_beat(session: AsyncSession) -> datetime | None:
    stored = await session.get(Setting, STATE_KEY)
    if stored is None or not isinstance(stored.value, dict):
        return None
    raw = stored.value.get("last_beat")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def record_beat(session: AsyncSession, now: datetime) -> None:
    stored = await session.get(Setting, STATE_KEY)
    value = {"last_beat": now.isoformat()}
    if stored is None:
        session.add(Setting(key=STATE_KEY, value=value))
    else:
        stored.value = {**(stored.value or {}), **value}


async def detect_gap(
    session: AsyncSession, now: datetime, threshold: timedelta = GAP_THRESHOLD
) -> tuple[datetime, datetime] | None:
    last = await load_last_beat(session)
    if last is None or now - last <= threshold:
        return None
    return last, now


def downtime_message(since: datetime, until: datetime, tz: ZoneInfo) -> str:
    seconds = int((until - since).total_seconds())
    start = since.astimezone(tz)
    end = until.astimezone(tz)
    same_day = start.date() == end.date()
    span = (
        f"{start:%d.%m %H:%M} – {end:%H:%M}"
        if same_day
        else f"{start:%d.%m %H:%M} – {end:%d.%m %H:%M}"
    )
    return (
        f"⚠️ <b>Бот не работал {fmt_duration(seconds)}</b> ({span}).\n\n"
        "Сообщения за время простоя (до суток) Telegram хранит — они уже догружены, "
        "обращения пересчитаны. Алерты по просрочкам, случившимся в простое, "
        "уйдут сейчас, если ещё актуальны.\n\n"
        "Если это не плановый перезапуск — стоит посмотреть сервер: "
        "«Состояние системы» покажет, что с ИИ и очередью."
    )
