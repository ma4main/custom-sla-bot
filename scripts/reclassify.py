"""Переспросить ИИ по уже классифицированным сообщениям — ОСОЗНАННО.

Смена промпта прошлое не трогает: новые правила действуют вперёд.
Этот скрипт — единственный способ переспросить историю, поэтому требует
явного `--confirm`. Последствия:

  • расход токенов на каждое сообщение;
  • ЦИФРЫ ЗА ПРОШЛЫЕ ПЕРИОДЫ ИЗМЕНЯТСЯ: вердикты перепишутся, эпизоды
    пересоберутся, отчёты за прошлые периоды станут другими.

Сообщениям ставится `needs_reclassification`, дальше воркер штатно
удаляет старый вердикт и переспрашивает пачками; прерванная обработка
продолжится следующим тиком.

Запуск на сервере (скрипты в образ не копируются — их подключают томом):

    cd /opt/chat-sla-bot && docker compose run --rm --no-deps \
      -v /opt/chat-sla-bot/scripts:/app/scripts -e PYTHONPATH=/app \
      bot python scripts/reclassify.py --side company --confirm

Без `--confirm` печатает, сколько сообщений затронет, и выходит.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from app.db.base import session_scope
from app.db.models import BusinessSide, Message


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="только сообщения с этой даты (ГГГГ-ММ-ДД); без неё — вся история",
    )
    parser.add_argument(
        "--side",
        choices=("client", "company", "all"),
        default="all",
        help="чьи сообщения переспрашивать",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="выполнить; без флага — только показать масштаб",
    )
    return parser.parse_args()


def _conditions(args: argparse.Namespace) -> list:
    conditions = [Message.text.isnot(None)]
    if args.side == "client":
        conditions.append(Message.business_side == BusinessSide.CLIENT)
    elif args.side == "company":
        conditions.append(Message.business_side == BusinessSide.COMPANY)
    else:
        conditions.append(
            Message.business_side.in_([BusinessSide.CLIENT, BusinessSide.COMPANY])
        )
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        conditions.append(Message.sent_at >= since)
    return conditions


async def main() -> None:
    args = _parse_args()
    conditions = _conditions(args)

    async with session_scope() as session:
        total = await session.scalar(
            select(func.count(Message.id)).where(*conditions)
        )
        if not args.confirm:
            print(f"Затронет сообщений: {total}")
            print(
                "Это перепишет вердикты и цифры за прошлые периоды. "
                "Запустите с --confirm, если именно этого и хотите."
            )
            return

        await session.execute(
            update(Message).where(*conditions).values(needs_reclassification=True)
        )
        print(f"Помечено на переспрос: {total}. Воркер переспросит пачками.")


if __name__ == "__main__":
    asyncio.run(main())
