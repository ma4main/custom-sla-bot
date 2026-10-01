"""Доставка алерта нескольким получателям: успех у одного — не успех.

Событие закрывается только доставкой всем адресатам; повтор идёт тем, кому
не дошло. Отдельно — личные алерты сотруднику и отправка после ночной тишины.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import (
    AlertLog,
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.alerts import process_alerts, retry_undelivered
from app.services.settings_store import set_value
from app.services.tracking import open_period
from tests.conftest import requires_db

# Понедельник, 13:00 МСК — рабочее время при графике по умолчанию,
# чтобы окно тишины не проглотило отправку.
NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)

GOOD = 900001  # этому доставляется
BAD = 900002  # этому бот «заблокирован»


class FlakyBot:
    """Бот, у которого часть адресатов всегда падает.

    Считает попытки по получателям, чтобы тест мог убедиться: повтор ушёл
    только тому, кому в прошлый раз не дошло.
    """

    def __init__(self, failing: set[int] = frozenset({BAD})) -> None:
        self.attempts: list[int] = []
        self.failing = failing

    async def send_message(self, chat_id: int, *args, **kwargs) -> None:
        self.attempts.append(chat_id)
        if chat_id in self.failing:
            raise RuntimeError("Forbidden: bot was blocked by the user")


async def _two_recipients_and_overdue_episode(session) -> Chat:
    for tg_id in (GOOD, BAD):
        session.add(
            BotUser(
                tg_user_id=tg_id,
                role=BotRole.OWNER,
                permissions={},
                state=BotUserState.ACTIVE,
            )
        )

    chat = Chat(tg_chat_id=-100999001, title="Доставка", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))

    # Обращение в 10:00 МСК, порог 30 минут — к NOW срок давно вышел.
    opened_at = NOW - timedelta(hours=2)
    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Когда будет акт сверки?",
        char_count=23,
        sent_at=opened_at,
    )
    session.add(message)
    await session.flush()

    session.add(
        Interaction(
            chat_id=chat.id,
            opened_at=opened_at,
            opened_by_message_id=message.id,
            last_client_at=opened_at,
            client_messages=1,
            state=InteractionState.OPEN,
        )
    )
    await set_value(session, "alerts", "enabled", True, actor_id=None)
    await set_value(session, "alerts", "respect_quiet_hours", False, actor_id=None)
    await session.flush()
    return chat


@requires_db
async def test_partial_delivery_does_not_close_the_alert(session, monkeypatch):
    """Дошло одному из двух — событие остаётся незакрытым."""
    await _two_recipients_and_overdue_episode(session)
    bot = FlakyBot()

    await process_alerts(session, bot)
    await session.flush()

    entry = await session.scalar(select(AlertLog))
    assert entry is not None, "алерт вообще не сработал — проверь порог и график"
    assert entry.recipients == [GOOD]
    assert entry.delivered is False, (
        "успех у одного адресата закрыл событие целиком: второй не получит "
        "алерт никогда, retry_undelivered берёт только delivered=False"
    )


NEW_OK = 900003
NEW_BAD = 900004


@requires_db
async def test_swapped_recipients_do_not_close_event(session):
    """Состав адресатов сменился между попытками — длины врут, множества нет.

    Старый доставленный ID не должен закрывать событие за нового
    недоставленного.
    """
    await _two_recipients_and_overdue_episode(session)
    await process_alerts(session, FlakyBot())  # GOOD доставлен, BAD нет
    await session.flush()

    # GOOD и BAD ушли из получателей, пришли двое новых.
    for user in (await session.scalars(select(BotUser))).all():
        if user.tg_user_id in (GOOD, BAD):
            user.state = BotUserState.DISABLED
    for tg_id in (NEW_OK, NEW_BAD):
        session.add(
            BotUser(
                tg_user_id=tg_id,
                role=BotRole.OWNER,
                permissions={},
                state=BotUserState.ACTIVE,
            )
        )
    await session.flush()

    await retry_undelivered(session, FlakyBot(failing={NEW_BAD}))
    await session.flush()

    entry = await session.scalar(select(AlertLog))
    assert NEW_OK in entry.recipients, "новый адресат не получил повтор"
    assert entry.delivered is False, (
        "старый доставленный ID закрыл событие за нового недоставленного: "
        "длины совпали, а множества — нет"
    )


@requires_db
async def test_retry_reaches_only_the_one_who_missed_it(session, monkeypatch):
    """Повтор идёт тому, кому не дошло, и не задваивает первому."""
    await _two_recipients_and_overdue_episode(session)
    bot = FlakyBot()

    await process_alerts(session, bot)
    await session.flush()
    bot.attempts.clear()

    await retry_undelivered(session, bot)
    await session.flush()

    assert bot.attempts == [BAD], "повтор ушёл не тому или задвоился"
    entry = await session.scalar(select(AlertLog))
    assert entry.delivered is False, "второму по-прежнему не дошло"
    assert entry.attempts >= 1


# ═══════════════════════════════════════════════════════════════
# «Свои алерты» сотруднику
# ═══════════════════════════════════════════════════════════════

MANAGER_TG = 900010


class RecordingBot:
    """Бот, который записывает, кому и с какими кнопками отправлено."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, object]] = []

    async def send_message(self, chat_id: int, *args, **kwargs) -> None:
        self.sent.append((chat_id, kwargs.get("reply_markup")))


async def _handoff_overdue_episode(session, *, link_manager: bool) -> None:
    """Обращение с передачей, у которой срок специалиста давно вышел."""
    from app.db.models import Staff

    session.add(
        BotUser(
            tg_user_id=GOOD, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
        )
    )
    manager = Staff(full_name="Ирина Передатчица", normalized_name="ирина передатчица")
    session.add(manager)
    await session.flush()
    if link_manager:
        session.add(
            BotUser(
                tg_user_id=MANAGER_TG,
                role=BotRole.MANAGER,
                permissions={},
                state=BotUserState.ACTIVE,
                staff_id=manager.id,
            )
        )

    chat = Chat(tg_chat_id=-100999010, title="Передача", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=9))

    opened_at = NOW - timedelta(days=6)  # вторник прошлой недели — срок давно вышел
    message = Message(
        chat_id=chat.id,
        tg_message_id=77,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Посчитайте налог, пожалуйста",
        char_count=28,
        sent_at=opened_at,
    )
    session.add(message)
    await session.flush()
    session.add(
        Interaction(
            chat_id=chat.id,
            opened_at=opened_at,
            opened_by_message_id=message.id,
            last_client_at=opened_at,
            client_messages=1,
            state=InteractionState.REACTED,
            first_reaction_at=opened_at + timedelta(minutes=5),
            first_reaction_staff_id=manager.id,
            handoff_at=opened_at + timedelta(minutes=5),
            handoff_staff_id=manager.id,
        )
    )
    await set_value(session, "alerts", "enabled", True, actor_id=None)
    await set_value(session, "alerts", "substantive_mode", "on", actor_id=None)
    await set_value(session, "alerts", "respect_quiet_hours", False, actor_id=None)
    await session.flush()


@requires_db
async def test_manager_gets_own_handoff_alert_without_button(session):
    """Передавший менеджер получает алерт «нет ответа специалиста».

    В дополнение к владельцам, не вместо них; без кнопки «Показать
    переписку» — у роли нет права на выписку, кнопка отвечала бы отказом.
    """
    await _handoff_overdue_episode(session, link_manager=True)
    bot = RecordingBot()

    await process_alerts(session, bot)
    await session.flush()

    recipients = {chat_id for chat_id, _ in bot.sent}
    assert GOOD in recipients, "владелец перестал получать алерт"
    assert MANAGER_TG in recipients, "менеджер не получил свой алерт"

    markup_by_id = dict(bot.sent)
    assert markup_by_id[GOOD] is not None, "у владельца пропала кнопка переписки"
    assert markup_by_id[MANAGER_TG] is None, "сотруднику ушла кнопка-отказ"

    entry = await session.scalar(select(AlertLog))
    assert set(entry.recipients) == {GOOD, MANAGER_TG}
    assert entry.delivered is True


@requires_db
async def test_no_personal_alert_without_linked_account(session):
    """Учётка не привязана — алерт уходит только владельцам, без падений."""
    await _handoff_overdue_episode(session, link_manager=False)
    bot = RecordingBot()

    await process_alerts(session, bot)
    await session.flush()

    recipients = {chat_id for chat_id, _ in bot.sent}
    assert recipients == {GOOD}


@requires_db
async def test_night_alert_goes_out_at_the_start_of_the_day(session, monkeypatch):
    """Ночью молчим, а с открытием дня накопленное уходит сразу, без задержки."""
    from app.services import alerts as alerts_module

    await _handoff_overdue_episode(session, link_manager=False)
    await set_value(session, "alerts", "respect_quiet_hours", True, actor_id=None)
    await session.flush()

    frozen = {"now": datetime(2026, 8, 24, 5, 30, tzinfo=timezone.utc)}  # 08:30 МСК

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return frozen["now"]

    monkeypatch.setattr(alerts_module, "datetime", _Frozen)
    bot = RecordingBot()

    await process_alerts(session, bot)
    await session.flush()
    assert bot.sent == [], "до открытия рабочего дня алерты молчат"
    assert await session.scalar(select(AlertLog)) is None, (
        "событие зафиксировано до открытия дня — второй тик его уже не пошлёт"
    )

    frozen["now"] = datetime(2026, 8, 24, 7, 10, tzinfo=timezone.utc)  # 10:10 МСК
    await process_alerts(session, bot)
    await session.flush()

    assert {chat_id for chat_id, _ in bot.sent} == {GOOD}, (
        "с началом рабочего дня накопленное за ночь обязано уйти"
    )
