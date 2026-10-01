"""Журнал апдейтов не теряет апдейт.

Сериализация входящего апдейта не падает на сентинелах aiogram
(`Default(...)`), которыми библиотека заполняет незаданные поля, — иначе
апдейт погибал бы целиком, до записи в журнал и до обработчиков.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from aiogram.types import Update
from sqlalchemy import select

import app.bot.middlewares as mw
from app.db.models import TelegramUpdate
from tests.conftest import requires_db


def _callback_update(update_id: int = 999_001) -> Update:
    """Нажатие кнопки на сообщении с отключённым превью ссылки.

    Собирается из JSON — ровно так, как апдейт приходит от Telegram:
    именно на валидации библиотека и подставляет сентинелы в поля,
    которых в JSON не было.
    """
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 1000000001, "is_bot": False, "first_name": "Павел"},
                "chat_instance": "1",
                "data": "user:list",
                "message": {
                    "message_id": 10,
                    "date": 1756300000,
                    "chat": {"id": 1000000001, "type": "private"},
                    "text": "Приглашение",
                    "link_preview_options": {"is_disabled": True},
                },
            },
        }
    )


def test_update_with_link_preview_is_serializable():
    payload = mw.dump_update(_callback_update())

    # Главное: не упало. И результат обязан быть настоящим JSON — payload
    # едет в JSONB, где объекты библиотеки не переживут запись.
    assert json.dumps(payload)
    assert payload["callback_query"]["message"]["link_preview_options"] == {"is_disabled": True}


def test_journal_keeps_only_what_telegram_sent():
    """Придуманные библиотекой умолчания в сырьё не попадают.

    Журнал ценен тем, что в нём лежит присланное: по нему разбирают
    инциденты и переигрывают события.
    """
    message = mw.dump_update(_callback_update())["callback_query"]["message"]

    assert "text" in message
    assert "prefer_small_media" not in message["link_preview_options"]


@requires_db
async def test_update_reaches_handler_and_journal(session, monkeypatch):
    """Поведенческая проверка: апдейт записан И обработан."""

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(mw, "session_scope", fake_scope)

    seen: list[Update] = []

    async def handler(event: Update, data: dict) -> str:
        seen.append(event)
        return "ok"

    update = _callback_update(999_002)
    result = await mw.RawUpdateJournalMiddleware()(handler, update, {})

    assert result == "ok", "обработчик не был вызван — кнопка бы не работала"
    assert seen and seen[0].update_id == 999_002
    stored = await session.scalar(
        select(TelegramUpdate).where(TelegramUpdate.update_id == 999_002)
    )
    assert stored is not None, "апдейт не попал в журнал"
    assert stored.payload["callback_query"]["data"] == "user:list"


@requires_db
async def test_broken_payload_costs_the_payload_not_the_update(session, monkeypatch):
    """Сбой сериализации стоит сырья по одному апдейту, а не самого апдейта.

    Остаётся заглушка с текстом ошибки — видно, что разобрать не удалось, —
    а обработка идёт своим чередом.
    """

    @asynccontextmanager
    async def fake_scope():
        yield session

    def boom(event: Update) -> dict:
        raise TypeError("неизвестный тип поля")

    monkeypatch.setattr(mw, "session_scope", fake_scope)
    monkeypatch.setattr(mw, "dump_update", boom)

    called = False

    async def handler(event: Update, data: dict) -> str:
        nonlocal called
        called = True
        return "ok"

    result = await mw.RawUpdateJournalMiddleware()(handler, _callback_update(999_003), {})

    assert called and result == "ok"
    stored = await session.scalar(
        select(TelegramUpdate).where(TelegramUpdate.update_id == 999_003)
    )
    assert stored.payload["_unserializable"] is True
    assert "TypeError" in stored.payload["error"]


@requires_db
async def test_duplicate_update_is_not_processed_twice(session, monkeypatch):
    """Идемпотентность сохраняется и на заглушке: ключ — update_id."""

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(mw, "session_scope", fake_scope)

    calls = 0

    async def handler(event: Update, data: dict) -> str:
        nonlocal calls
        calls += 1
        return "ok"

    middleware = mw.RawUpdateJournalMiddleware()
    await middleware(handler, _callback_update(999_004), {})
    await middleware(handler, _callback_update(999_004), {})

    assert calls == 1, "повторная доставка обработана второй раз"


# ── Ошибка SQL не срывает приём и переигровку ─────────────────────────────
async def _bad_sql(session, *args, **kwargs):
    from sqlalchemy import text

    await session.execute(text("SELECT * FROM no_such_table_for_test"))


def _integrator_message(chat_id: int, message_id: int) -> dict:
    return {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "supergroup", "title": "Синтетика"},
        "from": {"id": 9870044, "is_bot": True},
        "date": 1788249600,
        "text": "Синтетика Человек [corp.example] пишет:\nСчёт отправили",
    }


@requires_db
async def test_attribution_sql_error_does_not_lose_the_message(session, monkeypatch):
    """Атрибуция упала ошибкой SQL — сообщение сохраняется, транзакция приёма жива."""
    from sqlalchemy import func

    from app.config import get_settings
    from app.db.models import Message
    from app.services import attribution
    from app.services.ingestion import ingest_message

    monkeypatch.setattr(get_settings(), "integrator_bot_id", 9870044)
    monkeypatch.setattr(attribution, "attribute_message", _bad_sql)

    message = await ingest_message(session, _integrator_message(-100987004, 1))
    await session.flush()
    stored = await session.scalar(select(func.count()).select_from(Message).where(Message.id == message.id))
    assert stored == 1, "ошибка атрибуции погубила сообщение"


@requires_db
async def test_replay_sql_error_keeps_other_updates(session, monkeypatch):
    """Ошибка SQL одного апдейта: соседний переигран, у упавшего записана ошибка."""
    from datetime import datetime, timedelta, timezone

    from app.config import get_settings
    from app.services import ingestion
    from app.services.reprocess import replay_failed_updates

    monkeypatch.setattr(get_settings(), "integrator_bot_id", 9870044)
    real_ingest = ingestion.ingest_message

    async def first_breaks(session, raw, **kwargs):
        if raw["message_id"] == 1:
            await _bad_sql(session)
        return await real_ingest(session, raw, **kwargs)

    monkeypatch.setattr(ingestion, "ingest_message", first_breaks)
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    for update_id, message_id in ((9870441, 1), (9870442, 2)):
        session.add(
            TelegramUpdate(
                update_id=update_id,
                update_type="message",
                payload={"message": _integrator_message(-100987005, message_id)},
                received_at=old,
                processing_error="сбой при приёме",
            )
        )
    await session.flush()

    result = await replay_failed_updates(session)
    await session.flush()
    assert result["succeeded"] == 1 and result["failed"] == 1, result
    broken = await session.get(TelegramUpdate, 9870441)
    replayed = await session.get(TelegramUpdate, 9870442)
    assert "no_such_table_for_test" in (broken.processing_error or "")
    assert replayed.processing_error is None and replayed.processed_at is not None
