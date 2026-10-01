"""Алерт ждёт вердикта у сообщения, возвращённого в очередь.

Движок без вердикта считает, что ответ нужен: сообщение, чей вердикт стёрт
и переспрашивается, не должно будить людей до ответа модели.
"""

from sqlalchemy import select

from app.config import get_settings
from app.db.models import AlertLog, Classification, Message
from app.services.alerts import process_alerts
from tests.conftest import requires_db
from tests.test_alert_delivery import FlakyBot, _two_recipients_and_overdue_episode


@requires_db
async def test_message_back_in_queue_holds_the_alert(session):
    chat = await _two_recipients_and_overdue_episode(session)
    message = await session.scalar(select(Message).where(Message.chat_id == chat.id))
    message.needs_reclassification = True
    await session.flush()

    bot = FlakyBot(failing=set())
    await process_alerts(session, bot)
    await session.flush()
    assert await session.scalar(select(AlertLog)) is None, (
        "сообщение ждёт переклассификации — алерт обязан подождать вердикта"
    )
    assert bot.attempts == []

    # Вердикт пришёл (пусть даже «ответ нужен») — теперь алерт уходит.
    session.add(
        Classification(
            message_id=message.id,
            model=get_settings().ai_model,
            prompt_version=9,
            source="model",
            label="question",
            requires_response=True,
        )
    )
    await session.flush()
    await process_alerts(session, bot)
    await session.flush()
    assert await session.scalar(select(AlertLog)) is not None
    assert bot.attempts, "после вердикта алерт должен уйти"


@requires_db
async def test_fresh_message_without_verdict_still_alerts(session):
    """Обычный путь не меняется: свежее сообщение без вердикта будит."""
    await _two_recipients_and_overdue_episode(session)
    bot = FlakyBot(failing=set())
    await process_alerts(session, bot)
    await session.flush()
    assert await session.scalar(select(AlertLog)) is not None
