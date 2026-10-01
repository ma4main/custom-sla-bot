"""Группа «Уведомления по чатам».

Единственная группа, куда бот пишет: SendMessage (алерты, сводки),
SendDocument (файлы отчётов) и правка своего текста. Всё остальное
блокируется даже для неё, любая другая группа остаётся полностью закрытой.

Группа уведомлений исключена из аналитики целиком: её сообщения не
инжестятся — иначе собственный алерт бота через 30 минут породил бы
алерт о самом себе.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.bot.guard import blocked_reason
from app.config import get_settings
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
from app.services.alerts import process_alerts
from app.services.ingestion import ingest_message
from app.services.settings_store import set_value
from app.services.tracking import open_period
from tests.conftest import requires_db

NOTIFY = -5551112223
OWNER = 910001

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)


# ── Заслон: чистая функция решения ─────────────────────────────


def test_notify_group_allows_only_text_document_and_edit():
    assert blocked_reason("SendMessage", NOTIFY, None, NOTIFY) is None
    assert blocked_reason("SendDocument", NOTIFY, None, NOTIFY) is None
    # Правка своего текста — зачёркивание отработанного алерта: менеджеры
    # работают по ленте группы и должны видеть, что уже закрыто.
    assert blocked_reason("EditMessageText", NOTIFY, None, NOTIFY) is None
    for method in ("SendPhoto", "PinChatMessage", "DeleteMessage", "EditMessageCaption",
                   "EditMessageReplyMarkup", "BanChatMember", "SendPoll", "SendVideoNote"):
        assert blocked_reason(method, NOTIFY, None, NOTIFY) is not None, method


def test_other_groups_stay_fully_closed():
    assert blocked_reason("SendMessage", -100123, None, NOTIFY) is not None
    assert blocked_reason("SendDocument", -100123, None, NOTIFY) is not None
    # Послабление для группы уведомлений не должно течь на чужие группы:
    # править чужую ленту бот не может так же, как и писать в неё.
    assert blocked_reason("EditMessageText", -100123, None, NOTIFY) is not None
    # Чтение — для любых групп.
    assert blocked_reason("GetChat", -100123, None, NOTIFY) is None


def test_exception_requires_explicit_setting():
    """Без NOTIFY_GROUP_CHAT_ID исключения не существует."""
    assert blocked_reason("SendMessage", NOTIFY, None, None) is not None


def test_string_chat_id_never_matches_notify_group():
    """.env хранит числовой id; строка «-555…» — не группа уведомлений."""
    assert blocked_reason("SendMessage", str(NOTIFY), None, NOTIFY) is not None


def test_private_chats_unaffected():
    assert blocked_reason("SendMessage", 12345, None, NOTIFY) is None


# ── Исключение из аналитики ────────────────────────────────────


@requires_db
async def test_notify_group_message_is_not_ingested(session, monkeypatch):
    """Сообщение из группы уведомлений не создаёт ни Message, ни Chat."""
    monkeypatch.setattr(get_settings(), "notify_group_chat_id", NOTIFY)

    result = await ingest_message(
        session,
        {
            "message_id": 1,
            "date": int(NOW.timestamp()),
            "chat": {"id": NOTIFY, "type": "supergroup", "title": "Уведомления по чатам"},
            "from": {"id": 555, "is_bot": False, "first_name": "Клиент"},
            "text": "🔴 1 · Нет реакции",
        },
    )
    await session.flush()

    assert result is None
    chat = await session.scalar(select(Chat).where(Chat.tg_chat_id == NOTIFY))
    assert chat is None, "для группы уведомлений завёлся чат — она попала в аналитику"


# ── Доставка алертов в группу ──────────────────────────────────


async def _owner_and_overdue_episode(session) -> None:
    session.add(
        BotUser(
            tg_user_id=OWNER,
            role=BotRole.OWNER,
            permissions={},
            state=BotUserState.ACTIVE,
        )
    )
    chat = Chat(tg_chat_id=-100999002, title="Клиентский", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=NOW - timedelta(days=1))

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


class RecordingBot:
    """Записывает отправки вместе с наличием клавиатуры."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, bool]] = []

    async def send_message(self, chat_id: int, *args, reply_markup=None, **kwargs) -> None:
        self.sent.append((chat_id, reply_markup is not None))


@requires_db
async def test_alert_goes_to_group_in_addition_to_owners(session, monkeypatch):
    """Флаг включён — алерт уходит и в личку, и в группу; в группу без кнопок."""
    monkeypatch.setattr(get_settings(), "notify_group_chat_id", NOTIFY)
    await _owner_and_overdue_episode(session)
    await set_value(session, "alerts", "to_group", True, actor_id=None)
    await session.flush()

    bot = RecordingBot()
    await process_alerts(session, bot)
    await session.flush()

    targets = dict(bot.sent)
    assert OWNER in targets and targets[OWNER] is True, "личка без кнопки переписки"
    assert NOTIFY in targets, "алерт не дошёл до группы уведомлений"
    assert targets[NOTIFY] is False, (
        "в группе оказалась кнопка: бот не должен предлагать там диалог"
    )

    entry = await session.scalar(select(AlertLog))
    assert entry.delivered is True
    assert sorted(entry.recipients) == sorted([OWNER, NOTIFY])


@requires_db
async def test_alert_skips_group_when_flag_off(session, monkeypatch):
    """Флаг выключен (по умолчанию) — группа не получает ничего."""
    monkeypatch.setattr(get_settings(), "notify_group_chat_id", NOTIFY)
    await _owner_and_overdue_episode(session)

    bot = RecordingBot()
    await process_alerts(session, bot)
    await session.flush()

    assert [chat_id for chat_id, _ in bot.sent] == [OWNER]


@requires_db
async def test_alert_skips_group_when_env_missing(session, monkeypatch):
    """Флаг включён, но группа не задана на сервере — шлём только в лички."""
    monkeypatch.setattr(get_settings(), "notify_group_chat_id", None)
    await _owner_and_overdue_episode(session)
    await set_value(session, "alerts", "to_group", True, actor_id=None)
    await session.flush()

    bot = RecordingBot()
    await process_alerts(session, bot)
    await session.flush()

    assert [chat_id for chat_id, _ in bot.sent] == [OWNER]
