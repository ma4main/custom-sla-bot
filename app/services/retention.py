"""Очистка старых данных по срокам хранения (раздел настроек retention).

  telegram_update — записи старше `raw_update_days` удаляются; проекция
    сообщения остаётся, теряется только возможность переиграть событие;
  message.text — тексты старше `message_text_days` обнуляются; счётчики, авторство,
    классификации и обращения остаются.
`message_text_days = None` — бессрочно: тексты нужны для пересборки атрибуции.

Очистка текста влияет на пересчёт стороны отправителя (reprocess.recompute_sides):
сообщения без текста и медиа считаются служебными.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, TelegramUpdate
from app.services.settings_store import get_section

log = structlog.get_logger(__name__)

# Строк за одну пачку: воркер не должен залипать на чистке.
BATCH = 5000
# Пачек за один суточный проход (до 100 тыс. строк).
MAX_ROUNDS = 20


async def apply_retention(session: AsyncSession) -> dict[str, int]:
    """Один проход очистки. Возвращает, сколько чего удалено."""
    cfg = await get_section(session, "retention")
    now = datetime.now(timezone.utc)
    removed_updates = 0
    cleared_texts = 0

    # Пачками до исчерпания просроченного: одна пачка в сутки не успевала бы за потоком.
    # MAX_ROUNDS — предохранитель от залипания тика.
    raw_days = cfg.get("raw_update_days")
    if isinstance(raw_days, int) and raw_days > 0:
        cutoff = now - timedelta(days=raw_days)
        for _ in range(MAX_ROUNDS):
            ids = (
                await session.scalars(
                    select(TelegramUpdate.update_id)
                    .where(TelegramUpdate.received_at < cutoff)
                    .limit(BATCH)
                )
            ).all()
            if not ids:
                break
            await session.execute(
                delete(TelegramUpdate).where(TelegramUpdate.update_id.in_(ids))
            )
            removed_updates += len(ids)
            if len(ids) < BATCH:
                break

    text_days = cfg.get("message_text_days")
    if isinstance(text_days, int) and text_days > 0:
        cutoff = now - timedelta(days=text_days)
        for _ in range(MAX_ROUNDS):
            ids = (
                await session.scalars(
                    select(Message.id)
                    .where(Message.sent_at < cutoff)
                    .where(Message.text.isnot(None))
                    .limit(BATCH)
                )
            ).all()
            if not ids:
                break
            # Обнуление, а не удаление строки: на сообщение ссылаются атрибуция и обращения.
            await session.execute(
                update(Message).where(Message.id.in_(ids)).values(text=None)
            )
            cleared_texts += len(ids)
            if len(ids) < BATCH:
                break

    if removed_updates or cleared_texts:
        log.info(
            "retention.applied",
            removed_updates=removed_updates,
            cleared_texts=cleared_texts,
        )
    return {"removed_updates": removed_updates, "cleared_texts": cleared_texts}
