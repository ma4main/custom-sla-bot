"""Сотрудник пишет в чат напрямую: активная учётка бота — сторона компании,
даже без привязки к справочнику. Иначе его реплики становятся клиентскими,
и система жалуется, что компания молчит.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import (
    Attribution,
    AttributionMethod,
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    Message,
    Staff,
    TransportActorKind,
)
from tests.conftest import requires_db

NOW = datetime(2026, 9, 8, 10, 17, tzinfo=timezone.utc)
OWNER_TG = 100200330
STRANGER_TG = 100200331
FIRED_TG = 100200332
OWNER_TEXT = "Здравствуйте, нам нужно пораньше"


def _account(tg_user_id: int, *, role: BotRole, state: BotUserState, staff_id: int | None = None):
    return BotUser(
        tg_user_id=tg_user_id,
        role=role,
        permissions={},
        state=state,
        staff_id=staff_id,
    )


@requires_db
async def test_active_bot_account_is_company_without_staff_link(session):
    """Главный случай: учётка есть, привязки к справочнику нет."""
    from app.services.ingestion import resolve_business_side

    session.add(_account(OWNER_TG, role=BotRole.OWNER, state=BotUserState.ACTIVE))
    await session.flush()

    side = await resolve_business_side(session, TransportActorKind.HUMAN_USER, OWNER_TG, OWNER_TEXT)
    assert side is BusinessSide.COMPANY, "владелец без привязки снова считается клиентом"

    unknown = await resolve_business_side(
        session, TransportActorKind.HUMAN_USER, 100200339, OWNER_TEXT
    )
    assert unknown is BusinessSide.CLIENT, "человек без учётки — по-прежнему клиент"


@requires_db
async def test_pending_account_stays_client(session):
    """Запись PENDING заводится любому, кто нажал /start, — в том числе клиенту."""
    from app.services.ingestion import resolve_business_side

    session.add(_account(STRANGER_TG, role=BotRole.MANAGER, state=BotUserState.PENDING))
    await session.flush()

    side = await resolve_business_side(
        session, TransportActorKind.HUMAN_USER, STRANGER_TG, "Добрый день!"
    )
    assert side is BusinessSide.CLIENT, "нажатие /start не делает человека сотрудником"


@requires_db
async def test_disabled_account_keeps_company_only_when_linked(session):
    """Отключение учётки не должно задним числом делать уволенного клиентом.

    Но отключают и отклонённые заявки незнакомцев — их сторону трогать нельзя.
    """
    from app.services.ingestion import resolve_business_side

    person = Staff(full_name="Уволенный Сотрудник", normalized_name="уволенный сотрудник")
    session.add(person)
    await session.flush()
    session.add(
        _account(FIRED_TG, role=BotRole.MANAGER, state=BotUserState.DISABLED, staff_id=person.id)
    )
    session.add(_account(STRANGER_TG, role=BotRole.MANAGER, state=BotUserState.DISABLED))
    await session.flush()

    fired = await resolve_business_side(session, TransportActorKind.HUMAN_USER, FIRED_TG, "текст")
    assert fired is BusinessSide.COMPANY, "история уволенного стала бы обращениями клиента"
    rejected = await resolve_business_side(
        session, TransportActorKind.HUMAN_USER, STRANGER_TG, "текст"
    )
    assert rejected is BusinessSide.CLIENT, "отклонённая заявка — не сотрудник"


@requires_db
async def test_direct_message_signed_through_bot_account_link(session):
    """В выписке прямое сообщение подписано именем: привязка учётки — тоже путь."""
    from app.services.attribution import attribute_message

    person = Staff(full_name="Дмитрий Кузнецов", normalized_name="дмитрий кузнецов")
    session.add(person)
    await session.flush()
    session.add(
        _account(OWNER_TG, role=BotRole.OWNER, state=BotUserState.ACTIVE, staff_id=person.id)
    )
    chat = Chat(tg_chat_id=-100980002, title="Бухгалтерия: ИП Смирнов + ООО Вектор")
    session.add(chat)
    await session.flush()
    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        tg_user_id=OWNER_TG,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.COMPANY,
        text=OWNER_TEXT,
        char_count=len(OWNER_TEXT),
        sent_at=NOW,
    )
    session.add(message)
    await session.flush()

    await attribute_message(session, message)
    await session.flush()

    row = await session.get(Attribution, message.id)
    assert row is not None and row.staff_id == person.id, "автор остался нераспознанным"
    assert row.method is AttributionMethod.TG_ID


@requires_db
async def test_link_marks_old_direct_messages_with_author(session):
    """Привязка учётки к сотруднику подписывает и уже накопленное — без
    перезапуска воркера и без кнопки «разметить авторов»."""
    from app.services.access import link_staff

    admin_person = Staff(full_name="Павел Никитин", normalized_name="павел никитин")
    person = Staff(full_name="Дмитрий Кузнецов", normalized_name="дмитрий кузнецов")
    session.add_all([admin_person, person])
    await session.flush()
    actor = _account(100200337, role=BotRole.OWNER, state=BotUserState.ACTIVE)
    target = _account(OWNER_TG, role=BotRole.OWNER, state=BotUserState.ACTIVE)
    session.add_all([actor, target])
    chat = Chat(tg_chat_id=-100980004, title="Бухгалтерия: ИП Смирнов + ООО Вектор")
    session.add(chat)
    await session.flush()
    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        tg_user_id=OWNER_TG,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.COMPANY,
        text=OWNER_TEXT,
        char_count=len(OWNER_TEXT),
        sent_at=NOW,
    )
    session.add(message)
    await session.flush()

    await link_staff(session, actor, target, person.id)
    await session.flush()

    assert target.role is BotRole.OWNER, "привязка не должна трогать роль"
    assert target.state is BotUserState.ACTIVE, "привязка не должна трогать доступ"
    row = await session.get(Attribution, message.id)
    assert row is not None and row.staff_id == person.id, "старое сообщение осталось без автора"


@requires_db
async def test_recompute_returns_owner_messages_to_company(session, monkeypatch):
    """Пересчёт правила версии 3 чинит уже накопленное: реплики владельца
    становятся сообщениями компании, а клиентский вердикт по ним стирается —
    их переспросят промптом компании."""
    from app.config import get_settings
    from app.services.ingestion import SIDE_RULE_VERSION
    from app.services.reprocess import recompute_sides

    session.add(_account(OWNER_TG, role=BotRole.OWNER, state=BotUserState.ACTIVE))
    chat = Chat(
        tg_chat_id=-100980003,
        title="Бухгалтерия: ИП Смирнов + ООО Вектор",
        state=ChatState.TRACKED,
    )
    session.add(chat)
    await session.flush()
    owner_msg = Message(
        chat_id=chat.id,
        tg_message_id=1,
        tg_user_id=OWNER_TG,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        side_rule_version=2,
        text=OWNER_TEXT,
        char_count=len(OWNER_TEXT),
        sent_at=NOW,
        needs_reclassification=False,
    )
    client_msg = Message(
        chat_id=chat.id,
        tg_message_id=2,
        tg_user_id=100200338,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        side_rule_version=2,
        text="Добрый день! Выписку сможем прислать только 16.09",
        char_count=48,
        sent_at=NOW,
        needs_reclassification=False,
    )
    session.add_all([owner_msg, client_msg])
    await session.flush()
    model = get_settings().ai_model
    session.add_all(
        [
            Classification(
                message_id=owner_msg.id,
                model=model,
                prompt_version=9,
                source="model",
                label="request",
                requires_response=True,
            ),
            Classification(
                message_id=client_msg.id,
                model=model,
                prompt_version=9,
                source="model",
                label="info",
                requires_response=False,
            ),
        ]
    )
    await session.flush()

    result = await recompute_sides(session)
    assert result["reclassify"] >= 1

    await session.refresh(owner_msg)
    await session.refresh(client_msg)
    assert owner_msg.business_side is BusinessSide.COMPANY
    assert owner_msg.side_rule_version == SIDE_RULE_VERSION
    assert owner_msg.needs_reclassification is True
    assert client_msg.business_side is BusinessSide.CLIENT, "настоящий клиент не пострадал"
    assert client_msg.needs_reclassification is False

    remaining = (
        await session.scalars(
            select(Classification.message_id).where(
                Classification.message_id.in_([owner_msg.id, client_msg.id])
            )
        )
    ).all()
    assert remaining == [client_msg.id], "клиентский вердикт по реплике владельца стёрт"
