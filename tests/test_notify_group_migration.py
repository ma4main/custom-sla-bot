"""Переезд группы уведомлений в супергруппу: самовосстановление.

При конвертации Telegram меняет номер группы; здесь закреплены все три пути
самовосстановления: узнавание нового номера, досыл алерта и заслон отправки.
"""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramMigrateToChat
from aiogram.methods import SendMessage

import app.config as config
from app.db.models import Setting
from app.services.alerts import _GroupTarget, _deliver
from app.services.notify_group import STATE_KEY, load_migration, record_migration
from tests.conftest import requires_db

OLD_ID = -5001112223
NEW_ID = -1009998887776


@pytest.fixture(autouse=True)
def _clean_override():
    """Override — процессное состояние: тесты не должны течь друг в друга."""
    config.set_notify_group_override(None)
    yield
    config.set_notify_group_override(None)


@requires_db
async def test_record_migration_updates_recognition_and_persists(session, monkeypatch):
    monkeypatch.setattr(
        config.get_settings(), "notify_group_chat_id", OLD_ID, raising=False
    )
    assert config.is_notify_group(OLD_ID)
    assert not config.is_notify_group(NEW_ID)

    assert await record_migration(session, NEW_ID) is True
    await session.flush()

    # Узнаются ОБА номера: старый мёртв, но его тоже не инжестим.
    assert config.is_notify_group(NEW_ID), "супергруппа не узнана — уйдёт в аналитику"
    assert config.is_notify_group(OLD_ID)
    assert config.effective_notify_group_id() == NEW_ID

    # Повтор — не событие: уведомление владельцам не должно дублироваться.
    assert await record_migration(session, NEW_ID) is False

    stored = await session.get(Setting, STATE_KEY)
    assert stored.value["migrated_to"] == NEW_ID


@requires_db
async def test_load_migration_restores_after_restart(session):
    session.add(Setting(key=STATE_KEY, value={"migrated_to": NEW_ID}))
    await session.flush()

    assert await load_migration(session) == NEW_ID
    assert config.effective_notify_group_id() == NEW_ID


@requires_db
async def test_deliver_resends_into_migrated_group(session, monkeypatch):
    """TelegramMigrateToChat при отправке — досыл в новый номер тем же тиком."""
    monkeypatch.setattr(
        config.get_settings(), "notify_group_chat_id", OLD_ID, raising=False
    )

    sent: list[int] = []

    class MigratingBot:
        async def send_message(self, chat_id: int, *args, **kwargs) -> None:
            if chat_id == OLD_ID:
                raise TelegramMigrateToChat(
                    method=SendMessage(chat_id=chat_id, text="x"),
                    message="migrate",
                    migrate_to_chat_id=NEW_ID,
                )
            sent.append(chat_id)

    delivered, error, message_ids = await _deliver(
        MigratingBot(), [_GroupTarget(tg_user_id=OLD_ID)], "алерт", None
    )

    assert NEW_ID in sent, "досыл в новый номер не случился"
    assert delivered == [OLD_ID], "учёт доставки должен вестись по исходному адресату"
    assert error is None
    # Зачёркивать потом придётся сообщение в НОВОМ чате — под его номером
    # id и запоминается.
    assert OLD_ID not in {int(key) for key in message_ids}
    assert config.effective_notify_group_id() == NEW_ID, "переезд не запомнился"


@requires_db
async def test_guard_follows_the_migrated_group(session, monkeypatch):
    """Заслон обязан пускать SendMessage в ПЕРЕЕХАВШИЙ номер, а не только в номер из конфига."""
    from app.bot.guard import OutboundGroupGuard, blocked_reason

    monkeypatch.setattr(
        config.get_settings(), "notify_group_chat_id", OLD_ID, raising=False
    )
    guard = OutboundGroupGuard(resolver=config.effective_notify_group_id)
    assert guard._current_group_id() == OLD_ID

    await record_migration(session, NEW_ID)
    assert guard._current_group_id() == NEW_ID, "заслон не увидел переезд"
    assert blocked_reason("SendMessage", NEW_ID, None, guard._current_group_id()) is None
    assert blocked_reason("SendMessage", OLD_ID, None, guard._current_group_id()) is not None, (
        "старый номер после переезда — чужая группа"
    )
