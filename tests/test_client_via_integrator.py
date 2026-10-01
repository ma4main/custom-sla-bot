"""Клиент через портал интегратора: сообщение бота-интегратора с подписью
«(К) Имя [corp.example.com] пишет:» — сторона клиента."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import (
    Attribution,
    AttributionMethod,
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    Message,
    Setting,
    TransportActorKind,
)
from tests.conftest import requires_db

NOW = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
INTEGRATOR = 7000000002
CLIENT_TEXT = "(К) Людмила [corp.example.​com] пишет:   Посмотрите, пожалуйста, приняли ли упд?"
STAFF_TEXT = "Ирина Соколова [corp.example.​com] пишет:   Добрый день, минуту проверю"


def test_client_mark_in_signature():
    from app.services.attribution import is_client_signature, parse_integrator_prefix

    assert is_client_signature(parse_integrator_prefix(CLIENT_TEXT))
    assert is_client_signature(parse_integrator_prefix("(К) Светлана [corp.example.com] делится файлом"))
    assert is_client_signature("( к ) Анна Руденко"), "пробелы и регистр не должны мешать"
    assert is_client_signature("(K) Latin"), "латинская K — та же метка"
    assert not is_client_signature(parse_integrator_prefix(STAFF_TEXT))
    assert not is_client_signature("Кристина Сергеева"), "имя на К — не метка"
    assert not is_client_signature(None)


@pytest.mark.asyncio
async def test_integrator_message_with_client_mark_is_client_side(monkeypatch):
    from app.config import get_settings
    from app.services.ingestion import resolve_business_side

    monkeypatch.setattr(get_settings(), "integrator_bot_id", INTEGRATOR)
    # Ветка интегратора в базу не ходит — сессия не нужна.
    client = await resolve_business_side(None, TransportActorKind.INTEGRATOR_BOT, INTEGRATOR, CLIENT_TEXT)
    assert client is BusinessSide.CLIENT
    staff = await resolve_business_side(None, TransportActorKind.INTEGRATOR_BOT, INTEGRATOR, STAFF_TEXT)
    assert staff is BusinessSide.COMPANY
    service = await resolve_business_side(
        None, TransportActorKind.INTEGRATOR_BOT, INTEGRATOR, "Бот подключён к чату"
    )
    assert service is BusinessSide.INTEGRATOR_SYSTEM
    # Без префикса автор неизвестен — остаётся компания и unresolved.
    bare = await resolve_business_side(None, TransportActorKind.INTEGRATOR_BOT, INTEGRATOR, "текст")
    assert bare is BusinessSide.COMPANY


@requires_db
async def test_recompute_flips_side_and_returns_message_to_queue(session, monkeypatch):
    """Старый вердикт получен промптом компании — стирается; сообщение
    ждёт классификации как клиентское даже в архивном чате; строка
    атрибуции «(К) Людмила» уходит из нераспознанных."""
    from app.config import get_settings
    from app.services.ai_stats import pending_conditions
    from app.services.ingestion import SIDE_RULE_VERSION
    from app.services.reprocess import recompute_sides

    monkeypatch.setattr(get_settings(), "integrator_bot_id", INTEGRATOR)
    chat = Chat(tg_chat_id=-100980001, title="Бухгалтерия: ООО Вектор", state=ChatState.ARCHIVED)
    session.add(chat)
    await session.flush()
    client_msg = Message(
        chat_id=chat.id, tg_message_id=1, tg_user_id=INTEGRATOR,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY, side_rule_version=1,
        text=CLIENT_TEXT, char_count=len(CLIENT_TEXT), sent_at=NOW,
        needs_reclassification=False,
    )
    staff_msg = Message(
        chat_id=chat.id, tg_message_id=2, tg_user_id=INTEGRATOR,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY, side_rule_version=1,
        text=STAFF_TEXT, char_count=len(STAFF_TEXT), sent_at=NOW,
        needs_reclassification=False,
    )
    session.add_all([client_msg, staff_msg])
    await session.flush()
    model = get_settings().ai_model
    session.add_all(
        [
            Classification(
                message_id=client_msg.id, model=model, prompt_version=9,
                source="model", label="question", is_substantive=False,
            ),
            Classification(
                message_id=staff_msg.id, model=model, prompt_version=9,
                source="model", label="ack", is_substantive=False,
            ),
            Attribution(
                message_id=client_msg.id, staff_id=None, method=None,
                confidence=None, parser_version=2, raw_name="(К) Людмила",
            ),
        ]
    )
    await session.flush()

    result = await recompute_sides(session)
    assert result["reclassify"] == 1

    await session.refresh(client_msg)
    await session.refresh(staff_msg)
    assert client_msg.business_side is BusinessSide.CLIENT
    assert client_msg.side_rule_version == SIDE_RULE_VERSION
    assert client_msg.needs_reclassification is True
    assert staff_msg.business_side is BusinessSide.COMPANY, "сотрудник не пострадал"
    assert staff_msg.needs_reclassification is False

    remaining = (
        await session.scalars(
            select(Classification.message_id).where(
                Classification.message_id.in_([client_msg.id, staff_msg.id])
            )
        )
    ).all()
    assert remaining == [staff_msg.id], "чужой вердикт клиента стёрт, вердикт сотрудника цел"
    assert await session.get(Attribution, client_msg.id) is None
    pending = (
        await session.scalars(
            select(Message.id).where(*pending_conditions(model)).where(Message.chat_id == chat.id)
        )
    ).all()
    assert pending == [client_msg.id], "перевёрнутое сообщение ждёт клиентского промпта"


@requires_db
async def test_manual_attribution_survives_recompute(session, monkeypatch):
    from app.config import get_settings
    from app.services.reprocess import recompute_sides

    monkeypatch.setattr(get_settings(), "integrator_bot_id", INTEGRATOR)
    chat = Chat(tg_chat_id=-100980002, title="Чат", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    message = Message(
        chat_id=chat.id, tg_message_id=1, tg_user_id=INTEGRATOR,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY, side_rule_version=1,
        text=CLIENT_TEXT, char_count=len(CLIENT_TEXT), sent_at=NOW,
    )
    session.add(message)
    await session.flush()
    session.add(
        Attribution(
            message_id=message.id, staff_id=None, method=AttributionMethod.MANUAL,
            confidence=1.0, parser_version=2, raw_name="(К) Людмила",
        )
    )
    await session.flush()

    await recompute_sides(session)
    assert await session.get(Attribution, message.id) is not None, "решение человека не стирается"


@requires_db
async def test_side_rule_bump_recomputes_on_worker_start(session, monkeypatch):
    """Запись состояния без версии правила — версия 1: первый старт после
    выката пересчитывает историю сам и запоминает новую версию."""
    from app.config import get_settings
    from app.services.ingestion import SIDE_RULE_VERSION
    from app.services.reprocess import INTEGRATOR_STATE_KEY, sync_integrator_change

    monkeypatch.setattr(get_settings(), "integrator_bot_id", INTEGRATOR)
    chat = Chat(tg_chat_id=-100980003, title="Чат", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    message = Message(
        chat_id=chat.id, tg_message_id=1, tg_user_id=INTEGRATOR,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY, side_rule_version=1,
        text=CLIENT_TEXT, char_count=len(CLIENT_TEXT), sent_at=NOW,
    )
    session.add(message)
    session.add(Setting(key=INTEGRATOR_STATE_KEY, value={"integrator_bot_id": INTEGRATOR}))
    await session.flush()

    assert await sync_integrator_change(session) is True
    await session.refresh(message)
    assert message.business_side is BusinessSide.CLIENT
    state = await session.get(Setting, INTEGRATOR_STATE_KEY)
    assert state.value["side_rule_version"] == SIDE_RULE_VERSION
    assert state.value["integrator_bot_id"] == INTEGRATOR

    assert await sync_integrator_change(session) is False, "второй старт ничего не пересчитывает"
