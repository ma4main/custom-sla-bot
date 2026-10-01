"""Отработанный алерт зачёркивается в самом сообщении.

Правила, которые тест держит:
  - правим ОДИН раз на алерт, даже если Telegram отказал (иначе тик каждую
    минуту ломился бы в сообщение, которое править нельзя);
  - «остались без ответа» НЕ зачёркиваем — то же правило, что в сводке:
    худший исход не прячем;
  - группу уведомлений правим тоже, но БЕЗ кнопок, и только действующую:
    в чужую группу и в старый номер после переезда не лезем.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db.models import (
    AlertLog,
    BotRole,
    BotUser,
    BotUserState,
    BreachDismissal,
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.alerts import KIND_NO_REACTION, strike_closed_alerts
from app.services.staff import normalize_name
from app.services.tracking import open_period
from tests.conftest import requires_db

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
OWNER_TG = 910001
GROUP_TG = -1009500777

SENT_TEXT = (
    "🔴 <b>1 · Нет реакции</b>\n"
    "Просрочено на <b>1 ч 00 мин</b> — ответить надо было до сегодня в 12:00\n"
    "\n"
    "Чат: <b>Сигма</b>\n"
    "\n"
    "👤 <b>Клиент</b> — сегодня в 11:30\n"
    "«Вопрос по акту»"
)


class RecordingBot:
    """Считает правки. `failing` — чаты, на которых Telegram «отказывает»."""

    def __init__(self, failing: set[int] = frozenset()) -> None:
        self.edits: list[dict] = []
        self.failing = failing

    async def edit_message_text(self, **kwargs) -> None:
        if kwargs["chat_id"] in self.failing:
            raise RuntimeError("Bad Request: message can't be edited")
        self.edits.append(kwargs)


async def _case(
    session,
    tg_chat_id: int,
    title: str,
    *,
    message_ids: dict | None = None,
    sent_text: str | None = SENT_TEXT,
    alert_sent_at: datetime | None = None,
    opened_at: datetime | None = None,
    **inter,
) -> tuple[Chat, Message]:
    chat = Chat(tg_chat_id=tg_chat_id, title=title, state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    opened_at = opened_at or NOW - timedelta(hours=2)
    await open_period(session, chat, reason="test", at=opened_at - timedelta(days=1))
    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Вопрос по акту",
        char_count=14,
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
            **inter,
        )
    )
    session.add(
        AlertLog(
            chat_id=chat.id,
            opened_by_message_id=message.id,
            kind=KIND_NO_REACTION,
            recipients=[OWNER_TG],
            delivered=True,
            message_ids=message_ids if message_ids is not None else {str(OWNER_TG): 555},
            sent_text=sent_text,
            **({"sent_at": alert_sent_at} if alert_sent_at is not None else {}),
        )
    )
    await session.flush()
    return chat, message


@requires_db
async def test_worked_alert_is_struck(session):
    person = Staff(full_name="Ирина Соколова", normalized_name=normalize_name("Ирина"))
    session.add(person)
    await session.flush()
    # Время алерта задаётся явно, а зачёркивание не трогает алерты старше
    # 48 часов — поэтому кейс живёт в настоящем «сейчас», а не в NOW.
    opened = datetime.now(timezone.utc) - timedelta(hours=2)
    await _case(
        session,
        -100960001,
        "Сигма",
        opened_at=opened,
        # Алерт ушёл по сроку в 30 минут, ответили через 42 — спустя 12 после алерта.
        alert_sent_at=opened + timedelta(minutes=30),
        state=InteractionState.ANSWERED,
        first_reaction_at=opened + timedelta(minutes=42),
        first_reaction_staff_id=person.id,
        ttfr_seconds=42 * 60,
        ttfr_business_seconds=40 * 60,
    )
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 1

    assert len(bot.edits) == 1
    edit = bot.edits[0]
    assert edit["chat_id"] == OWNER_TG and edit["message_id"] == 555
    text = edit["text"]
    assert text.startswith("🟢 <b>1 · Нет реакции — отработано</b>"), (
        "красный кружок обязан смениться зелёным"
    )
    assert "<s>" in text and "Вопрос по акту" in text, "тело зачёркнуто, но осталось"
    assert "Менеджер отработал: ответил(а) Ирина Соколова" in text
    assert "за 40 мин рабочего времени, или спустя 12 мин после алерта" in text, (
        "календарное время от обращения заменено временем после алерта"
    )
    assert "календарных" not in text
    assert edit["reply_markup"] is not None, "кнопка выписки в личке остаётся"


@requires_db
async def test_dismissed_alert_is_struck_as_manual(session):
    owner = BotUser(
        tg_user_id=OWNER_TG,
        display_name="Дмитрий",
        role=BotRole.OWNER,
        permissions={},
        state=BotUserState.ACTIVE,
    )
    session.add(owner)
    _, message = await _case(
        session, -100960002, "Ромашка", state=InteractionState.OPEN
    )
    session.add(
        BreachDismissal(
            chat_id=message.chat_id,
            opened_by_message_id=message.id,
            dismissed_by=owner.id,
        )
    )
    await session.flush()
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 1
    text = bot.edits[0]["text"]
    assert text.startswith("✋ <b>1 · Нет реакции — закрыто вручную</b>")
    assert "Закрыто вручную: Дмитрий." in text, (
        "решение человека должно быть подписано человеком"
    )


@requires_db
async def test_abandoned_alert_is_not_struck(session):
    """Худший исход не прячем — то же правило, что в сводке."""
    await _case(session, -100960003, "Северный ветер", state=InteractionState.ABANDONED)
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 0
    assert bot.edits == []
    entry = await session.scalar(select(AlertLog))
    assert entry.struck_at is not None, (
        "решение окончательное — иначе запись вечно занимала бы окно выборки "
        "и новые закрытия перестали бы обрабатываться"
    )


@requires_db
async def test_waiting_alert_is_not_touched(session):
    await _case(session, -100960004, "ООО Вектор", state=InteractionState.OPEN)
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 0
    assert bot.edits == []
    entry = await session.scalar(select(AlertLog))
    assert entry.struck_at is None, "горящий кейс ещё закроется — отметку не ставим"


@requires_db
async def test_strike_happens_once(session):
    """Второй тик не должен править то же сообщение снова."""
    opened = NOW - timedelta(hours=2)
    await _case(
        session,
        -100960005,
        "Сигма",
        state=InteractionState.ANSWERED,
        first_reaction_at=opened + timedelta(minutes=10),
        ttfr_seconds=600,
        ttfr_business_seconds=600,
    )
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 1
    assert await strike_closed_alerts(session, bot) == 0, "повторной правки быть не должно"
    assert len(bot.edits) == 1


@requires_db
async def test_refusal_does_not_break_the_tick(session):
    """Telegram отказал — тик живёт дальше, а алерт больше не трогаем."""
    opened = NOW - timedelta(hours=2)
    await _case(
        session,
        -100960006,
        "Сигма",
        state=InteractionState.ANSWERED,
        first_reaction_at=opened + timedelta(minutes=10),
        ttfr_seconds=600,
        ttfr_business_seconds=600,
    )
    bot = RecordingBot(failing={OWNER_TG})

    assert await strike_closed_alerts(session, bot) == 0
    entry = await session.scalar(select(AlertLog))
    assert entry.struck_at is not None, (
        "отметка ставится даже при отказе: иначе тик ломился бы каждую минуту"
    )
    assert "can't be edited" in (entry.last_error or "")


@requires_db
async def test_notify_group_message_is_edited_without_buttons(session, monkeypatch):
    """Группу уведомлений правим тоже — но без кнопок.

    Менеджеры работают по ленте группы и должны видеть, какие
    алерты уже закрыты. Кнопок там не было и быть не должно: бот в группе
    только публикует, нажатия не обслуживает.
    """
    import app.config as config

    # Переезд группы — процессное состояние: сбрасываем, чтобы чужой тест
    # не подменил действующий номер (тот же приём, что в тестах миграции).
    config.set_notify_group_override(None)
    monkeypatch.setattr(
        config.get_settings(), "notify_group_chat_id", GROUP_TG, raising=False
    )
    opened = NOW - timedelta(hours=2)
    await _case(
        session,
        -100960007,
        "Сигма",
        message_ids={str(OWNER_TG): 555, str(GROUP_TG): 42},
        state=InteractionState.ANSWERED,
        first_reaction_at=opened + timedelta(minutes=10),
        ttfr_seconds=600,
        ttfr_business_seconds=600,
    )
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 1
    markups = {edit["chat_id"]: edit["reply_markup"] for edit in bot.edits}
    assert markups[GROUP_TG] is None, "кнопки в группе не появляются"
    assert markups[OWNER_TG] is not None, "в личке кнопка выписки остаётся"


@requires_db
async def test_foreign_group_is_never_edited(session, monkeypatch):
    """Старый номер группы после переезда — уже не группа уведомлений.

    Заслон такую правку отвергнет, поэтому и пробовать нельзя: попытка
    писала бы в лог ERROR «outbound.blocked» и выглядела как инцидент.
    """
    import app.config as config

    config.set_notify_group_override(None)
    monkeypatch.setattr(
        config.get_settings(), "notify_group_chat_id", -1009500999, raising=False
    )
    opened = NOW - timedelta(hours=2)
    await _case(
        session,
        -100960010,
        "Сигма",
        message_ids={str(OWNER_TG): 555, str(GROUP_TG): 42},
        state=InteractionState.ANSWERED,
        first_reaction_at=opened + timedelta(minutes=10),
        ttfr_seconds=600,
        ttfr_business_seconds=600,
    )
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 1
    touched = {edit["chat_id"] for edit in bot.edits}
    assert touched == {OWNER_TG}, "чужую группу не трогаем"


@requires_db
async def test_old_alert_without_message_ids_is_skipped(session):
    """Алерт без сохранённых номеров сообщений править нечем — и это не ошибка."""
    opened = NOW - timedelta(hours=2)
    await _case(
        session,
        -100960008,
        "Старый",
        message_ids={},
        sent_text=None,
        state=InteractionState.ANSWERED,
        first_reaction_at=opened + timedelta(minutes=10),
        ttfr_seconds=600,
        ttfr_business_seconds=600,
    )
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 0
    assert bot.edits == []


@requires_db
async def test_alert_older_than_the_limit_is_not_edited(session):
    """Предел 48 часов: старое сообщение не трогаем вовсе.

    Ограничение осознанное: не полагаться на недокументированное поведение
    Telegram. Закрытие такого кейса видно в сводке.
    """
    from app.db.models import AlertLog as _AlertLog
    from sqlalchemy import update

    opened = NOW - timedelta(hours=2)
    await _case(
        session,
        -100960009,
        "Долгий",
        state=InteractionState.ANSWERED,
        first_reaction_at=opened + timedelta(minutes=10),
        ttfr_seconds=600,
        ttfr_business_seconds=600,
    )
    await session.execute(
        update(_AlertLog).values(
            sent_at=datetime.now(timezone.utc) - timedelta(hours=49)
        )
    )
    await session.flush()
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 0
    assert bot.edits == [], "правку старого сообщения даже не пробуем"
    entry = await session.scalar(select(AlertLog))
    assert entry.struck_at is not None, "моложе запись уже не станет — отмечаем"


@requires_db
async def test_a_gone_alert_waits_for_the_requests_it_covers(session):
    """«Обращения больше нет» не имеет права бросить покрытую просрочку.

    Покрытая просрочка оповещена ЭТИМ сообщением: как только покрывающий
    алерт перестал быть открытым, своего сообщения она не получит никогда —
    ключ `uq_alert_once` занят её же покрытой записью, и `_log_alert` вернёт
    None. Поэтому ветка `interaction is None` не ставит `struck_at`, пока
    покрытые ждут ответа.

    Сценарий: раннюю просьбу перевердиктили, и её сообщение обращения больше
    не открывает, а поздняя — по-прежнему ждёт ответа.
    """
    opened = datetime.now(timezone.utc) - timedelta(hours=2)
    chat = Chat(tg_chat_id=-100960010, title="Покрытие", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=opened - timedelta(days=1))

    def _message(tg_message_id: int, sent_at: datetime) -> Message:
        return Message(
            chat_id=chat.id,
            tg_message_id=tg_message_id,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
            business_side=BusinessSide.CLIENT,
            text="Вопрос по акту",
            char_count=14,
            sent_at=sent_at,
        )

    # Первая просьба: алерт по ней ушёл, обращения по ней больше нет.
    early = _message(1, opened)
    # Вторая просьба того же слоя: алерта своего не получила — покрыта.
    late = _message(2, opened + timedelta(minutes=40))
    session.add_all([early, late])
    await session.flush()
    session.add(
        Interaction(
            chat_id=chat.id,
            opened_at=late.sent_at,
            opened_by_message_id=late.id,
            last_client_at=late.sent_at,
            client_messages=1,
            state=InteractionState.OPEN,
        )
    )
    covering = AlertLog(
        chat_id=chat.id,
        opened_by_message_id=early.id,
        kind=KIND_NO_REACTION,
        recipients=[OWNER_TG],
        delivered=True,
        message_ids={str(OWNER_TG): 555},
        sent_text=SENT_TEXT,
        sent_at=opened + timedelta(minutes=30),
    )
    session.add(covering)
    await session.flush()
    session.add(
        AlertLog(
            chat_id=chat.id,
            opened_by_message_id=late.id,
            kind=KIND_NO_REACTION,
            recipients=[],
            delivered=True,
            # У покрытой записи своего сообщения нет — она «доставлена»
            # сообщением покрывающего алерта.
            message_ids={},
            sent_at=opened + timedelta(minutes=70),
            covered_by_id=covering.id,
        )
    )
    await session.flush()
    bot = RecordingBot()

    assert await strike_closed_alerts(session, bot) == 0
    assert bot.edits == [], "править нечего: обращения покрывающего уже нет"
    assert covering.struck_at is None, (
        "алерт закрыт, пока покрытая им просрочка ждёт ответа — "
        "своего сообщения она теперь не получит никогда"
    )

    # Покрытая закрылась — держать алерт больше не за чем.
    interaction = await session.scalar(
        select(Interaction).where(Interaction.opened_by_message_id == late.id)
    )
    interaction.state = InteractionState.ANSWERED
    interaction.first_reaction_at = late.sent_at + timedelta(minutes=5)
    await session.flush()

    assert await strike_closed_alerts(session, bot) == 0
    assert covering.struck_at is not None, (
        "запись обязана закрыться, иначе она вечно занимает окно выборки"
    )


async def test_closing_tail_without_staff_names_the_company():
    """Сотрудник не определён — приписка говорит «компания ответила»."""
    from types import SimpleNamespace

    from app.services.alerts import _closing_tail

    entry = SimpleNamespace(kind=KIND_NO_REACTION, sent_at=None)
    interaction = SimpleNamespace(
        first_reaction_staff_id=None,
        ttfr_business_seconds=40 * 60,
        first_reaction_at=None,
    )
    text = await _closing_tail(None, entry, interaction, "closed", NOW)
    assert text == "Менеджер отработал: компания ответила за 40 мин рабочего времени."
