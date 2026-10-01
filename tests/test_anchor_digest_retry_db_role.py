"""Якоря сообщений у обращения, досылка рассылок адресатам, которым не дошло,
роль приложения для базы.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    Message,
    Setting,
    TransportActorKind,
)
from tests.conftest import requires_db

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


# ── Якорь сообщения реакции ─────────────────────────────────────────────
@requires_db
async def test_reaction_anchor_points_to_the_exact_message(session):
    """Два сообщения компании в одну секунду: якорь однозначен, время — нет."""
    from app.services.episodes import rebuild_interactions
    from app.services.tracking import open_period

    chat = Chat(tg_chat_id=-100980001, title="Секунда", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))
    opened = NOW - timedelta(minutes=30)
    reply_at = opened + timedelta(minutes=5)
    session.add_all(
        [
            Message(
                chat_id=chat.id, tg_message_id=1, tg_user_id=7,
                transport_actor_kind=TransportActorKind.HUMAN_USER,
                business_side=BusinessSide.CLIENT, text="Где акт?", char_count=8, sent_at=opened,
            ),
            Message(
                chat_id=chat.id, tg_message_id=2, tg_user_id=8,
                transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
                business_side=BusinessSide.COMPANY, text="Добрый день", char_count=11,
                sent_at=reply_at,
            ),
            Message(
                chat_id=chat.id, tg_message_id=3, tg_user_id=8,
                transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
                business_side=BusinessSide.COMPANY, text="Акт во вложении", char_count=15,
                sent_at=reply_at,
            ),
        ]
    )
    await session.flush()

    await rebuild_interactions(session, now=NOW)

    interaction = await session.scalar(
        select(Interaction).where(Interaction.chat_id == chat.id)
    )
    assert interaction is not None and interaction.first_reaction_at == reply_at
    anchor = await session.get(Message, interaction.first_reaction_message_id)
    assert anchor is not None, "якорь реакции не записан"
    assert anchor.sent_at == interaction.first_reaction_at
    assert anchor.business_side is BusinessSide.COMPANY


# ── Досылка рассылок ────────────────────────────────────────────────────
@requires_db
async def test_failed_recipients_are_remembered_and_cleared(session, monkeypatch):
    from contextlib import asynccontextmanager

    from app.services import digest

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr("app.db.base.session_scope", fake_scope)

    await digest._remember_failures("weekly", [111, 222])
    await session.flush()
    pending = await digest.pending_retries(session)
    assert pending["weekly"]["targets"] == [111, 222]
    assert pending["weekly"]["attempts"] == 0

    await digest._remember_failures("weekly", [])
    await session.flush()
    assert "weekly" not in await digest.pending_retries(session)


@requires_db
async def test_send_issue_reports_partial_delivery(session):
    """Дошёл текст, не дошёл файл — адресат считается НЕдоставленным."""
    from pathlib import Path

    from app.services import digest

    class _Bot:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def send_message(self, chat_id, text, **kwargs):
            self.calls.append(("text", chat_id))

        async def send_document(self, chat_id, document, **kwargs):
            self.calls.append(("doc", chat_id))
            if chat_id == 222:
                raise RuntimeError("сеть")

    bot = _Bot()
    failed = await digest._send_issue(
        bot, "weekly", "отчёт", [(Path("x.xlsx"), "x.xlsx", "подпись")], [111, 222]
    )
    assert failed == [222], "половина отчёта — не отчёт"
    assert ("text", 111) in bot.calls and ("doc", 111) in bot.calls


# ── Роль приложения ─────────────────────────────────────────────────────
def test_runtime_dsn_prefers_app_role(monkeypatch):
    from app.config import Settings

    # APP_DATABASE_URL задаётся явно пустым: conftest кладёт его в окружение
    # (тестовая база), и без этого проверка зависела бы от среды запуска.
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://owner:x@db/one", APP_DATABASE_URL="", BOT_TOKEN="1:t"
    )
    assert settings.runtime_database_url() == "postgresql+asyncpg://owner:x@db/one"
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://owner:x@db/one",
        APP_DATABASE_URL="postgresql+asyncpg://botapp:y@db/one",
        BOT_TOKEN="1:t",
    )
    assert settings.runtime_database_url().startswith("postgresql+asyncpg://botapp:")
    assert settings.require_database_url().startswith("postgresql+asyncpg://owner:"), (
        "миграции остаются под владельцем схемы"
    )
