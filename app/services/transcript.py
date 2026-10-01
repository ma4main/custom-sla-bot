"""Расшифровка переписки: общие детали для алертов и кнопки «Показать переписку»,
чтобы стороны выглядели одинаково в обоих местах.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Attribution,
    BusinessSide,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.attribution import strip_integrator_prefix
from app.services.ingestion import ANONYMOUS_ADMIN_BOT_ID
from app.text import esc

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

MEDIA_LABELS = {
    "photo": "🖼 фото",
    "document": "📎 файл",
    "video": "🎬 видео",
    "voice": "🎤 голосовое",
    "audio": "🎵 аудио",
    "animation": "🎞 гиф",
    "sticker": "🙂 стикер",
    "video_note": "⏺ кружок",
    "service": "⚙️ служебное",
}

# Вложения, которые сами по себе — ответ (акт закрывает вопрос). Стикер, гиф, кружок — нет.
SUBSTANTIVE_MEDIA = frozenset({"document", "photo", "video", "audio", "voice"})

# Экранирование одно на весь проект (app/text.py).
escape = esc


def calendar_tz(calendar_cfg: dict[str, Any]) -> ZoneInfo:
    """Пояс рабочего календаря: время в алерте — то же, что в графике."""
    return ZoneInfo(calendar_cfg.get("timezone") or "Europe/Moscow")


def fmt_duration(seconds: int) -> str:
    # Меньше минуты — словами: «0 минут» выглядит как сломанный счётчик.
    if seconds < 60:
        return "меньше минуты"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч {minutes % 60:02d} мин"
    # От двух суток — днями.
    days, rest = divmod(hours, 24)
    return f"{days} дн {rest} ч"


def specialist_deadline_label(limit_minutes: int) -> str:
    """Срок второй ступени словами — по настоящему порогу, а не по умолчанию."""
    minutes = int(limit_minutes or 0)
    if minutes == 1440:
        return "то же время следующего рабочего дня"
    return f"{fmt_duration(minutes * 60)} после передачи"


def fmt_when(moment: datetime, tz: ZoneInfo, now: datetime) -> str:
    """«сегодня в 14:05», «вчера в 18:50», «пт 22.08 в 18:50»."""
    local = moment.astimezone(tz)
    days = (now.astimezone(tz).date() - local.date()).days
    if days == 0:
        return f"сегодня в {local:%H:%M}"
    if days == 1:
        return f"вчера в {local:%H:%M}"
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m} в {local:%H:%M}"


def media_mark(message: Message) -> str:
    if not message.has_media:
        return ""
    return MEDIA_LABELS.get(message.media_kind or "", "📎 вложение")


def snippet(message: Message, limit: int = 180) -> str:
    parts = []
    mark = media_mark(message)
    if mark:
        parts.append(f"[{mark}]")

    # Префикс интегратора убираем (автор подписан отдельно). Ничего не осталось —
    # хватит пометки вложения; нет и вложения — исходный текст.
    stripped = strip_integrator_prefix(message.text)
    text = stripped if (stripped or mark) else (message.text or "").strip()
    if text:
        cut = " ".join(text[:limit].split())
        if len(text) > limit:
            cut = cut.rstrip() + "…"
        parts.append(cut)
    if not parts:
        return "<i>(без текста)</i>"
    return escape(" ".join(parts))


def _thread_filter(query, thread_id: int | None, use_thread: bool):
    """Ветка форума; NULL сравнивается через IS. use_thread=False — обычная группа:
    Telegram ставит thread_id любому реплаю, и фильтр отрезал бы весь остальной чат.
    """
    if not use_thread:
        return query
    if thread_id is None:
        return query.where(Message.thread_id.is_(None))
    return query.where(Message.thread_id == thread_id)


def _with_author(query):
    """Имя автора: сотрудник из справочника, иначе имя из префикса интегратора."""
    return (
        query.add_columns(Staff.full_name, Attribution.raw_name)
        .outerjoin(Attribution, Attribution.message_id == Message.id)
        .outerjoin(Staff, Staff.id == Attribution.staff_id)
    )


def _author_of(row) -> str | None:
    return row[1] or row[2]


async def message_at(
    session: AsyncSession, chat_id: int, side: BusinessSide, moment: datetime
) -> Message | None:
    """Сообщение указанной стороны, отправленное в этот момент (обращение хранит времена;
    совпадение до секунды внутри чата однозначно).
    """
    return await session.scalar(
        select(Message)
        .where(Message.chat_id == chat_id)
        .where(Message.business_side == side)
        .where(Message.sent_at == moment)
        .order_by(Message.id)
        .limit(1)
    )


async def author_name(session: AsyncSession, message_id: int) -> str | None:
    row = (
        await session.execute(
            select(Staff.full_name, Attribution.raw_name)
            .select_from(Attribution)
            .outerjoin(Staff, Staff.id == Attribution.staff_id)
            .where(Attribution.message_id == message_id)
        )
    ).first()
    return (row[0] or row[1]) if row else None


async def last_company_message(
    session: AsyncSession,
    chat_id: int,
    thread_id: int | None,
    *,
    use_thread: bool,
) -> tuple[Message, str | None] | None:
    query = _with_author(
        select(Message)
        .where(Message.chat_id == chat_id)
        .where(Message.business_side == BusinessSide.COMPANY)
    )
    row = (
        await session.execute(
            _thread_filter(query, thread_id, use_thread)
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).first()
    return (row[0], _author_of(row)) if row else None


async def client_numbers(session: AsyncSession, chat_id: int) -> dict[int, int]:
    """Номера людей со стороны клиента: {tg_user_id: 1, 2, …}.

    Со стороны клиента в чате часто пишут двое-трое, и без номеров их разговор между
    собой выглядит как клиент, отвечающий сам себе. Номер — по первому сообщению
    в чате, поэтому человек везде (алерт, выписка, участники) под одним номером.
    Один человек — пусто: номер ему не нужен.
    """
    first = func.min(Message.id)
    rows = (
        await session.execute(
            select(Message.tg_user_id, first)
            .where(Message.chat_id == chat_id)
            .where(Message.business_side == BusinessSide.CLIENT)
            .where(Message.tg_user_id.isnot(None))
            .group_by(Message.tg_user_id)
            .order_by(first)
        )
    ).all()
    if len(rows) < 2:
        return {}
    return {int(row[0]): n for n, row in enumerate(rows, start=1)}


def client_label(message: Message, numbers: dict[int, int] | None) -> str:
    """«клиент» или «клиент 2» — строчными; заголовок алерта сам делает заглавную."""
    number = (numbers or {}).get(message.tg_user_id) if message.tg_user_id else None
    return f"клиент {number}" if number else "клиент"


async def load_around(
    session: AsyncSession,
    chat_id: int,
    thread_id: int | None,
    anchor_message_id: int,
    *,
    use_thread: bool,
    before: int = 3,
    after: int = 15,
) -> list[tuple[Message, str | None]]:
    """Переписка вокруг обращения: немного предыстории и всё, что было после.
    Показываются ВСЕ участники, не только клиент и компания; скрыты только служебные
    события Telegram без текста и вложения.
    """
    base = _with_author(
        select(Message)
        .where(Message.chat_id == chat_id)
        .where(
            or_(
                Message.transport_actor_kind != TransportActorKind.TELEGRAM_SYSTEM,
                Message.text.isnot(None),
                Message.has_media.is_(True),
            )
        )
    )

    earlier = (
        await session.execute(
            _thread_filter(base, thread_id, use_thread)
            .where(Message.id < anchor_message_id)
            .order_by(Message.id.desc())
            .limit(before)
        )
    ).all()
    later = (
        await session.execute(
            _thread_filter(base, thread_id, use_thread)
            .where(Message.id >= anchor_message_id)
            .order_by(Message.id)
            .limit(after)
        )
    ).all()

    return [(row[0], _author_of(row)) for row in reversed(earlier)] + [
        (row[0], _author_of(row)) for row in later
    ]


def side_label(
    message: Message, author: str | None, numbers: dict[int, int] | None = None
) -> str:
    """Подпись стороны в выписке (HTML). Стороны различаются ЦВЕТОМ кружка.
    `numbers` — из `client_numbers`: различает людей со стороны клиента."""
    if message.business_side is BusinessSide.CLIENT:
        return f"🔵 {client_label(message, numbers)}"
    if message.business_side is BusinessSide.INTEGRATOR_SYSTEM:
        # Ответ самого Битрикса на команду вроде /auth.
        return "⚙️ система (Битрикс)"
    if message.business_side is not BusinessSide.COMPANY:
        # Сторона не определена (чужой бот или служебное событие с текстом).
        return (
            "⚪ не определено (бот)"
            if message.transport_actor_kind is TransportActorKind.OTHER_BOT
            else "⚪ не определено"
        )
    if author:
        return f"🟢 {escape(author)}"
    if message.tg_user_id == ANONYMOUS_ADMIN_BOT_ID:
        # «От имени группы»: имени автора в сообщении физически нет.
        return "🟢 компания (анонимно)"
    return "🟢 компания"


def day_caption(moment: datetime, tz: ZoneInfo, now: datetime) -> str:
    """«сегодня», «вчера», «пт 22.08» — заголовок дня в выписке."""
    local = moment.astimezone(tz)
    days = (now.astimezone(tz).date() - local.date()).days
    if days == 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{WEEKDAYS[local.weekday()]} {local:%d.%m}"


def render_transcript(
    rows: list[tuple[Message, str | None]],
    tz: ZoneInfo,
    now: datetime,
    *,
    anchor_message_id: int | None = None,
    line_limit: int = 160,
    client_numbers: dict[int, int] | None = None,
) -> str:
    """`client_numbers` — номера людей со стороны клиента (см. `client_numbers`)."""
    lines: list[str] = []
    current_day = None

    for message, author in rows:
        local = message.sent_at.astimezone(tz)
        if local.date() != current_day:
            current_day = local.date()
            lines.append(f"\n<b>— {day_caption(message.sent_at, tz, now)} —</b>")

        mark = "▶️ " if message.id == anchor_message_id else ""
        lines.append(
            f"{mark}<code>{local:%H:%M}</code> {side_label(message, author, client_numbers)}: "
            f"{snippet(message, line_limit)}"
        )

    return "\n".join(lines).strip()
