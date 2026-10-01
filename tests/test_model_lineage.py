"""Смена модели ИИ не делает прошлую разметку «неразмеченной»: действительны
вердикты текущей модели и предыдущих (`AI_PREVIOUS_MODELS`); на одном сообщении
побеждает большая версия промпта."""

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.ai_stats import pending_count
from app.services.episodes import rebuild_interactions
from app.services.tracking import open_period
from tests.conftest import requires_db

OLD_MODEL = "vendor/previous-model"
GIGA = "ai-sage/GigaChat3.5-432B-A28B"

# Понедельник, 10:30 МСК — рабочее время при календаре по умолчанию.
OPENED_AT = datetime(2026, 8, 24, 7, 30, tzinfo=timezone.utc)
NOW = OPENED_AT + timedelta(hours=2)


def _switch(monkeypatch, *, model: str, previous: str) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_model", model, raising=False)
    monkeypatch.setattr(settings, "ai_previous_models", previous, raising=False)
    monkeypatch.setattr(settings, "ai_shadow_mode", False, raising=False)
    monkeypatch.setattr(settings, "ai_fallback_model", "", raising=False)


async def _chat(session, tg_chat_id: int) -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title="Переезд", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=OPENED_AT - timedelta(days=1))
    return chat


async def _client_message(session, chat: Chat, tg_message_id: int, text: str) -> Message:
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_message_id,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text=text,
        char_count=len(text),
        sent_at=OPENED_AT + timedelta(minutes=tg_message_id),
    )
    session.add(message)
    await session.flush()
    return message


def _verdict(message: Message, model: str, version: int, requires: bool) -> Classification:
    return Classification(
        message_id=message.id,
        model=model,
        prompt_version=version,
        source="model",
        label="request" if requires else "info",
        requires_response=requires,
    )


async def _state(session, chat: Chat) -> InteractionState:
    result = await rebuild_interactions(session, now=NOW, persist=False)
    [item] = [i for i in result["items"] if i.chat_id == chat.id]
    return item.state


def test_accepted_models_keeps_order_and_dedupes(monkeypatch):
    _switch(monkeypatch, model=GIGA, previous=f" {OLD_MODEL}, {GIGA},, ")
    assert get_settings().ai_accepted_models == [GIGA, OLD_MODEL]


@requires_db
async def test_newer_prompt_version_wins_across_models(session, monkeypatch):
    """Одно сообщение, два вердикта: прежняя модель v4 «ждёт», GigaChat v9 «нет»."""
    chat = await _chat(session, -100777401)
    message = await _client_message(session, chat, 1, "Сделайте счёт на аванс")
    session.add_all(
        [
            _verdict(message, OLD_MODEL, 4, True),
            _verdict(message, GIGA, 9, False),
        ]
    )
    await session.flush()

    _switch(monkeypatch, model=GIGA, previous=OLD_MODEL)
    assert await _state(session, chat) is InteractionState.NO_RESPONSE_NEEDED

    # Взгляд «только прежняя модель»: её вердикт по-прежнему читается.
    _switch(monkeypatch, model=OLD_MODEL, previous="")
    assert await _state(session, chat) is InteractionState.OPEN


@requires_db
async def test_history_classified_by_previous_model_stays_visible(session, monkeypatch):
    """Сообщение с вердиктом прежней модели после смены модели остаётся размеченным."""
    chat = await _chat(session, -100777402)
    message = await _client_message(session, chat, 1, "Спасибо, всё получили")
    session.add(_verdict(message, OLD_MODEL, 4, False))
    await session.flush()

    _switch(monkeypatch, model=GIGA, previous=OLD_MODEL)
    assert await _state(session, chat) is InteractionState.NO_RESPONSE_NEEDED

    # Без списка предыдущих моделей история слепнет, и «спасибо»
    # превращается в открытое обращение.
    _switch(monkeypatch, model=GIGA, previous="")
    assert await _state(session, chat) is InteractionState.OPEN


@requires_db
async def test_queue_does_not_requeue_history_after_switch(session, monkeypatch):
    """В очередь после смены модели попадает только новое, а не вся история."""
    chat = await _chat(session, -100777403)
    classified = await _client_message(session, chat, 1, "Пришлите акт сверки")
    await _client_message(session, chat, 2, "И счёт, пожалуйста")
    session.add(_verdict(classified, OLD_MODEL, 4, True))
    await session.flush()

    assert await pending_count(session, [GIGA, OLD_MODEL]) == 1
    assert await pending_count(session, [GIGA]) == 2
    # Строка вместо списка — прежний вызов продолжает работать.
    assert await pending_count(session, OLD_MODEL) == 1
