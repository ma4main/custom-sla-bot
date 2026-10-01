"""Вечерняя сводка: когда слать и что в ней написано.

Правило отправки — чистая функция should_send, перебирается без базы.
Содержимое и дедуп «раз в день» — поведенческие тесты с базой.
"""

from datetime import datetime, timedelta, timezone

from app.db.models import (
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    Staff,
    TransportActorKind,
)
from app.services.digest import (
    _monthly_due_key,
    _previous_month,
    should_send_monthly,
    build_digest,
    claim_evening_digest,
    claim_weekly_report,
    evening_payload,
    should_send,
    should_send_weekly,
    weekly_payload,
)
from app.services.settings_store import set_value
from app.services.transcript import calendar_tz
from app.services.tracking import open_period
from tests.conftest import requires_db

CFG_CAL = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "10:00",
    "end": "19:00",
    "timezone": "Europe/Moscow",
    "holidays": ["2026-08-25"],
}
CFG_ON = {"evening_enabled": True, "evening_time": "19:05", "to_group": False}

# Понедельник 24.08.2026. 19:10 МСК = 16:10 UTC.
MON_EVENING = datetime(2026, 8, 24, 16, 10, tzinfo=timezone.utc)
MON_NOON = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)
SAT_EVENING = datetime(2026, 8, 22, 16, 10, tzinfo=timezone.utc)
HOLIDAY_EVENING = datetime(2026, 8, 25, 16, 10, tzinfo=timezone.utc)


def test_should_send_matrix():
    assert should_send(MON_EVENING, CFG_ON, CFG_CAL, None) is True
    assert should_send(MON_EVENING, {**CFG_ON, "evening_enabled": False}, CFG_CAL, None) is False
    assert should_send(MON_NOON, CFG_ON, CFG_CAL, None) is False, "до 19:05 рано"
    assert should_send(SAT_EVENING, CFG_ON, CFG_CAL, None) is False, "суббота"
    assert should_send(HOLIDAY_EVENING, CFG_ON, CFG_CAL, None) is False, "праздник"
    assert should_send(MON_EVENING, CFG_ON, CFG_CAL, "2026-08-24") is False, "уже была"
    assert should_send(MON_EVENING, CFG_ON, CFG_CAL, "2026-08-21") is True, "вчерашняя не мешает"


def test_should_send_respects_custom_time():
    cfg = {**CFG_ON, "evening_time": "17:00"}
    at_1701 = datetime(2026, 8, 24, 14, 1, tzinfo=timezone.utc)  # 17:01 МСК
    at_1659 = datetime(2026, 8, 24, 13, 59, tzinfo=timezone.utc)
    assert should_send(at_1701, cfg, CFG_CAL, None) is True
    assert should_send(at_1659, cfg, CFG_CAL, None) is False


async def _company_message(session, chat, staff, sent_at, text="Готово, отправила"):
    from app.db.models import Attribution, AttributionMethod

    message = Message(
        chat_id=chat.id,
        tg_message_id=int(sent_at.timestamp()) % 1_000_000,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.COMPANY,
        text=text,
        char_count=len(text),
        sent_at=sent_at,
    )
    session.add(message)
    await session.flush()
    session.add(
        Attribution(
            message_id=message.id,
            staff_id=staff.id,
            method=AttributionMethod.EXACT,
            confidence=100,
        )
    )
    return message


async def _day_of_work(session) -> tuple[Chat, Staff, Staff]:
    """Один день: Ирина ответила с просрочкой, Нина молчала, одно
    обращение осталось без ответа."""
    chat = Chat(tg_chat_id=-100900201, title="ООО Вектор", state=ChatState.TRACKED)
    session.add(chat)
    busy = Staff(full_name="Ирина Соколова", normalized_name="ирина соколова", active=True)
    idle = Staff(full_name="Нина Козлова", normalized_name="нина козлова", active=True)
    session.add_all([busy, idle])
    await session.flush()
    await open_period(session, chat, reason="test", at=MON_EVENING - timedelta(days=2))

    opened_at = MON_EVENING - timedelta(hours=5)
    opener = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Пришлите акт",
        char_count=12,
        sent_at=opened_at,
    )
    session.add(opener)
    await session.flush()
    reply_at = opened_at + timedelta(hours=2)
    await _company_message(session, chat, busy, reply_at)
    session.add(
        Interaction(
            chat_id=chat.id,
            opened_at=opened_at,
            opened_by_message_id=opener.id,
            last_client_at=opened_at,
            client_messages=1,
            state=InteractionState.ANSWERED,
            first_reaction_at=reply_at,
            first_reaction_staff_id=busy.id,
            ttfr_business_seconds=7200,
            sla_breached=True,
        )
    )

    # Второе обращение — осталось без ответа.
    ghost_at = MON_EVENING - timedelta(hours=4)
    ghost = Message(
        chat_id=chat.id,
        tg_message_id=2,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Ау?",
        char_count=3,
        sent_at=ghost_at,
    )
    session.add(ghost)
    await session.flush()
    session.add(
        Interaction(
            chat_id=chat.id,
            opened_at=ghost_at,
            opened_by_message_id=ghost.id,
            last_client_at=ghost_at,
            client_messages=1,
            state=InteractionState.ABANDONED,
        )
    )
    await session.flush()
    return chat, busy, idle


@requires_db
async def test_digest_contents(session):
    await _day_of_work(session)

    text = await build_digest(session, CFG_CAL, MON_EVENING)

    assert "Ирина Соколова" in text and "нарушений: 1" in text
    # Сотрудник без активности показан, а не спрятан.
    assert "Нина Козлова — активности не было" in text
    assert "Осталось без ответа: 1" in text and "ООО Вектор" in text
    assert "Нарушения по чатам" in text
    # Честность атрибуции: сводка сама говорит о своём ограничении.
    assert "кто в итоге ответил" in text


@requires_db
async def test_digest_sends_once_per_day(session, monkeypatch):
    """Второй тик того же вечера сводку не задваивает."""
    from app.services import digest as digest_module

    await _day_of_work(session)
    session.add(
        BotUser(tg_user_id=910301, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    )
    await set_value(session, "digest", "evening_enabled", True, actor_id=None)
    await session.flush()

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return MON_EVENING

    monkeypatch.setattr(digest_module, "datetime", _FrozenDatetime)

    # Двухфазная схема: бронь дня — отдельно, отправка — отдельно.
    # Второй тик того же вечера брони не получает.
    assert await claim_evening_digest(session) is True
    text, targets = await evening_payload(session)
    assert targets == [910301]
    assert "Сводка за" in text

    assert await claim_evening_digest(session) is False


CFG_WEEKLY = {
    "weekly_enabled": True,
    "weekly_day": 1,
    "weekly_time": "10:05",
    "evening_enabled": False,
    "to_group": False,
}
# Понедельник 24.08, 10:10 МСК = 07:10 UTC.
MON_MORNING = datetime(2026, 8, 24, 7, 10, tzinfo=timezone.utc)
TUE_MORNING = datetime(2026, 8, 25, 7, 10, tzinfo=timezone.utc)


def test_should_send_weekly_matrix():
    assert should_send_weekly(MON_MORNING, CFG_WEEKLY, CFG_CAL, None) is True
    assert should_send_weekly(
        MON_MORNING, {**CFG_WEEKLY, "weekly_enabled": False}, CFG_CAL, None
    ) is False
    early = datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc)  # 10:00 МСК — рано
    assert should_send_weekly(early, CFG_WEEKLY, CFG_CAL, None) is False
    assert should_send_weekly(SAT_EVENING, CFG_WEEKLY, CFG_CAL, None) is False, "выходной"
    assert should_send_weekly(MON_MORNING, CFG_WEEKLY, CFG_CAL, "2026-W35") is False, (
        "на этой неделе уже была"
    )
    assert should_send_weekly(MON_MORNING, CFG_WEEKLY, CFG_CAL, "2026-W34") is True, (
        "прошлая неделя не мешает"
    )


def test_weekly_slips_to_next_workday_after_holiday():
    """День рассылки — праздник: отчёт уходит первым рабочим днём после."""
    cal = {**CFG_CAL, "holidays": ["2026-08-24"]}  # понедельник — праздник
    assert should_send_weekly(MON_MORNING, CFG_WEEKLY, cal, None) is False
    # Вторник: день прошёл, время суток уже не сверяется — шлём первым тиком.
    assert should_send_weekly(TUE_MORNING, CFG_WEEKLY, cal, None) is True
    at_night = datetime(2026, 8, 25, 4, 0, tzinfo=timezone.utc)  # 07:00 МСК вт
    assert should_send_weekly(at_night, CFG_WEEKLY, cal, None) is True


def test_weekly_saturday_setting_fires_on_monday():
    """Суббота в настройке при графике пн–пт — отчёт уходит в понедельник."""
    cfg = {**CFG_WEEKLY, "weekly_day": 6}
    assert should_send_weekly(SAT_EVENING, cfg, CFG_CAL, None) is False, "суббота нерабочая"
    # Понедельник: выпуск за субботу 22.08 (ISO-неделя W34) досылается.
    assert should_send_weekly(MON_MORNING, cfg, CFG_CAL, None) is True
    assert should_send_weekly(MON_MORNING, cfg, CFG_CAL, "2026-W34") is False, (
        "выпуск за эту субботу уже ушёл"
    )


@requires_db
async def test_weekly_report_sends_once_per_week(session, monkeypatch):
    from app.services import digest as digest_module

    await _day_of_work(session)
    session.add(
        BotUser(tg_user_id=910303, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    )
    await set_value(session, "digest", "weekly_enabled", True, actor_id=None)
    await session.flush()

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return MON_MORNING

    monkeypatch.setattr(digest_module, "datetime", _FrozenDatetime)

    assert await claim_weekly_report(session) is True
    text, targets, export, html_path = await weekly_payload(session)
    assert targets == [910303]
    assert "Отчёт за неделю" in text
    assert html_path is None, "HTML не заказан — файл не должен собираться"
    if export is not None:
        export[0].unlink(missing_ok=True)

    assert await claim_weekly_report(session) is False


@requires_db
async def test_weekly_html_attachment_follows_the_toggle(session, monkeypatch):
    """weekly_html включён — страница собирается; сам файл — валидный HTML."""
    from app.services import digest as digest_module

    await _day_of_work(session)
    session.add(
        BotUser(tg_user_id=910307, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    )
    await set_value(session, "digest", "weekly_enabled", True, actor_id=None)
    await set_value(session, "digest", "weekly_html", True, actor_id=None)
    await session.flush()

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return MON_MORNING

    monkeypatch.setattr(digest_module, "datetime", _FrozenDatetime)

    text, targets, export, html_path = await weekly_payload(session)
    try:
        assert html_path is not None, "флаг включён — страница обязана собраться"
        content = html_path.read_text(encoding="utf-8")
        assert "Аналитика чатов" in content
        assert "<main>" in content, "семантика — landmarks обязаны быть"
        assert "<thead>" in content, "семантика — шапки таблиц обязаны быть"
    finally:
        if export is not None:
            export[0].unlink(missing_ok=True)
        if html_path is not None:
            html_path.unlink(missing_ok=True)


@requires_db
async def test_weekly_and_evening_states_are_independent(session, monkeypatch):
    """Забронированная сводка не съедает недельный отчёт и наоборот."""
    from app.services import digest as digest_module

    await _day_of_work(session)
    session.add(
        BotUser(tg_user_id=910304, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    )
    await set_value(session, "digest", "evening_enabled", True, actor_id=None)
    await set_value(session, "digest", "weekly_enabled", True, actor_id=None)
    await session.flush()

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return MON_EVENING  # вечер понедельника: пора и сводке, и отчёту

    monkeypatch.setattr(digest_module, "datetime", _FrozenDatetime)

    assert await claim_evening_digest(session) is True
    assert await claim_weekly_report(session) is True


@requires_db
async def test_digest_reaches_group_when_enabled(session, monkeypatch):
    from app.config import get_settings
    from app.services import digest as digest_module

    await _day_of_work(session)
    session.add(
        BotUser(tg_user_id=910302, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    )
    await set_value(session, "digest", "evening_enabled", True, actor_id=None)
    await set_value(session, "digest", "to_group", True, actor_id=None)
    await session.flush()

    monkeypatch.setattr(get_settings(), "notify_group_chat_id", -5551112223)

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return MON_EVENING

    monkeypatch.setattr(digest_module, "datetime", _FrozenDatetime)

    assert await claim_evening_digest(session) is True
    _, targets = await evening_payload(session)
    assert sorted(targets) == [-5551112223, 910302]


# ═══════════════════════════════════════════════════════════════
# Ежемесячный отчёт
# ═══════════════════════════════════════════════════════════════

CFG_MONTHLY = {"monthly_enabled": True, "monthly_day": 1, "monthly_time": "10:05"}

# Вторник 1 сентября 2026, 10:10 МСК = 07:10 UTC.
SEP1_MORNING = datetime(2026, 9, 1, 7, 10, tzinfo=timezone.utc)
SEP1_EARLY = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)
AUG31_MORNING = datetime(2026, 8, 31, 7, 10, tzinfo=timezone.utc)


def test_should_send_monthly_matrix():
    assert should_send_monthly(SEP1_MORNING, CFG_MONTHLY, CFG_CAL, None) is True
    assert should_send_monthly(
        SEP1_MORNING, {**CFG_MONTHLY, "monthly_enabled": False}, CFG_CAL, None
    ) is False
    assert should_send_monthly(SEP1_EARLY, CFG_MONTHLY, CFG_CAL, None) is False, (
        "время рассылки ещё не наступило"
    )
    # Как у недельного: при ПЕРВОМ включении выпуск за последнее наступление
    # дня-цели уходит сразу, не дожидаясь следующего числа.
    assert should_send_monthly(AUG31_MORNING, CFG_MONTHLY, CFG_CAL, None) is True
    assert should_send_monthly(AUG31_MORNING, CFG_MONTHLY, CFG_CAL, "2026-08") is False, (
        "августовский выпуск уже уходил — до 1 сентября слать нечего"
    )
    assert should_send_monthly(SEP1_MORNING, CFG_MONTHLY, CFG_CAL, "2026-09") is False, (
        "дедуп: выпуск сентября уже уходил"
    )
    assert should_send_monthly(SEP1_MORNING, CFG_MONTHLY, CFG_CAL, "2026-08") is True, (
        "прошлый ключ — августовский, сентябрьский выпуск ещё не уходил"
    )


def test_monthly_slips_to_next_workday():
    """1-е выпало на выходной/праздник — выпуск уходит первым рабочим днём."""
    # 1 сентября объявлено праздником: досылка 2-го, время уже не сверяется.
    cal = {**CFG_CAL, "holidays": ["2026-09-01"]}
    sep2_early = datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)
    assert should_send_monthly(SEP1_MORNING, CFG_MONTHLY, cal, None) is False
    assert should_send_monthly(sep2_early, CFG_MONTHLY, cal, None) is True
    # Ключ дедупликации при досылке — тот же месяц-цель.
    tz_local = sep2_early.astimezone(calendar_tz(cal))
    assert _monthly_due_key(tz_local, 1) == "2026-09"


def test_previous_month_bounds():
    cal = CFG_CAL
    start, end = _previous_month(SEP1_MORNING, cal)
    tz = calendar_tz(cal)
    assert start.astimezone(tz).strftime("%d.%m %H:%M") == "01.08 00:00"
    assert end.astimezone(tz).strftime("%d.%m %H:%M") == "01.09 00:00"


@requires_db
async def test_brief_layout_drops_lists(session):
    """«Краткий» состав — без списков чатов и сотрудников, цифры на месте."""
    from app.bot.handlers.reports import _render_all
    from app.db.models import Attribution, Staff

    chat = Chat(tg_chat_id=-100550001, title="Краткость", state=ChatState.TRACKED)
    person = Staff(full_name="Полина Полная", normalized_name="полина полная")
    session.add_all([chat, person])
    await session.flush()
    await open_period(session, chat, reason="test", at=MON_NOON - timedelta(days=1))
    message = Message(
        chat_id=chat.id,
        tg_message_id=1,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY,
        text="ответ",
        char_count=5,
        sent_at=MON_NOON,
    )
    session.add(message)
    await session.flush()
    session.add(Attribution(message_id=message.id, staff_id=person.id, parser_version=2))
    await session.flush()

    start = MON_NOON - timedelta(hours=6)
    end = MON_NOON + timedelta(hours=6)

    full = await _render_all(session, start, end, layout="full")
    brief = await _render_all(session, start, end, layout="brief")

    assert "Краткость" in full and "Полина Полная" in full
    assert "Краткость" not in brief, "краткий состав показывает список чатов"
    assert "Полина Полная" not in brief, "краткий состав показывает список людей"
    assert "Сообщений:" in brief, "цифры из краткого состава пропали"
    assert "Активных чатов за период: 1" in brief


@requires_db
async def test_enabling_weekly_does_not_backfill(session, monkeypatch):
    """Включение рассылки не досылает прошлый выпуск.

    Включение помечает текущий выпуск отправленным, и первый настоящий
    уходит в следующий настроенный день.
    """
    from app.services.digest import _load_state, suppress_pending_issue

    await set_value(session, "digest", "weekly_enabled", True)
    await set_value(session, "digest", "weekly_day", 4)  # четверг
    await suppress_pending_issue(session, weekly=True)
    await session.flush()

    cfg = {"weekly_enabled": True, "weekly_day": 4, "weekly_time": "10:05"}
    last = await _load_state(session, "weekly_last_sent")
    assert last is not None, "включение не пометило текущий выпуск"

    # Прямо сейчас слать нечего — отметка совпадает с последним четвергом.
    now = datetime.now(timezone.utc)
    assert should_send_weekly(now, cfg, CFG_CAL, last) is False, (
        "рассылка ушла в момент включения"
    )

    # А через неделю после последнего наступления дня-цели — пора.
    next_due = now + timedelta(days=8)
    if should_send_weekly(next_due, cfg, CFG_CAL, last) is False:
        # Возможен выходной/праздник — сдвигаемся до рабочего дня.
        for shift in range(1, 4):
            if should_send_weekly(next_due + timedelta(days=shift), cfg, CFG_CAL, last):
                break
        else:
            raise AssertionError("следующий выпуск так и не наступил")


@requires_db
async def test_enabling_monthly_does_not_backfill(session):
    from app.services.digest import _load_state, suppress_pending_issue

    await set_value(session, "digest", "monthly_enabled", True)
    await suppress_pending_issue(session, monthly=True)
    await session.flush()

    cfg = {"monthly_enabled": True, "monthly_day": 1, "monthly_time": "10:05"}
    last = await _load_state(session, "monthly_last_sent")
    assert last is not None

    now = datetime.now(timezone.utc)
    assert should_send_monthly(now, cfg, CFG_CAL, last) is False, (
        "месячная рассылка ушла в момент включения"
    )


@requires_db
async def test_weekly_last7_without_xlsx(session, monkeypatch):
    """Период «последние 7 дней» и без XLSX — настройками, не правкой кода."""
    from app.services import digest as digest_module

    await _day_of_work(session)
    session.add(
        BotUser(tg_user_id=910309, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
    )
    await set_value(session, "digest", "weekly_enabled", True, actor_id=None)
    await set_value(session, "digest", "weekly_period", "last7", actor_id=None)
    await set_value(session, "digest", "weekly_xlsx", False, actor_id=None)
    await session.flush()

    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            return MON_MORNING

    monkeypatch.setattr(digest_module, "datetime", _FrozenDatetime)

    text, targets, export, html_path = await weekly_payload(session)
    assert "Отчёт за последние 7 дней" in text, "период обязан переключаться настройкой"
    assert "Отчёт за неделю" not in text
    assert export is None, "XLSX выключен настройкой — файла быть не должно"
    if html_path is not None:
        html_path.unlink(missing_ok=True)


# ── Сборка выпуска упала после брони ──────────────────────────────────────
def _state_store(monkeypatch, value: dict):
    """Строка состояния рассылок в памяти вместо базы."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.db import base

    stored = SimpleNamespace(value=value)
    session = SimpleNamespace(get=AsyncMock(return_value=stored), add=lambda row: None)

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(base, "session_scope", scope)
    return stored


async def test_build_failure_after_claim_goes_to_retry(monkeypatch):
    """Бронь уже взята, а XLSX не собрался: выпуск не потерян — он в досылке всем адресатам."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import digest

    stored = _state_store(monkeypatch, {"weekly_last_sent": "2026-W36"})
    monkeypatch.setattr(digest, "claim_weekly_report", AsyncMock(return_value=True))
    monkeypatch.setattr(digest, "weekly_payload", AsyncMock(side_effect=OSError("диск полон")))
    monkeypatch.setattr(digest, "get_section", AsyncMock(return_value={}))
    monkeypatch.setattr(digest, "_digest_targets", AsyncMock(return_value=[202, 101]))

    assert await digest.maybe_send_weekly_report(SimpleNamespace()) is False
    assert stored.value["retry"]["weekly"] == {"targets": [101, 202], "attempts": 0}

    # Следующий тик: сборка прошла — выпуск уходит тем же адресатам.
    monkeypatch.setattr(
        digest, "weekly_payload", AsyncMock(return_value=("Отчёт", [101, 202], None, None))
    )
    sender = AsyncMock(return_value=[])
    monkeypatch.setattr(digest, "_send_issue", sender)
    assert await digest.retry_undelivered_issues(SimpleNamespace()) == 2
    assert sender.await_args.args[-1] == [101, 202]
    assert "weekly" not in stored.value["retry"]


async def test_retry_build_failure_counts_as_attempt(monkeypatch):
    """Упавшая сборка при досылке — неудачная попытка, а не падение тика и не вечный повтор."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services import digest

    stored = _state_store(
        monkeypatch, {"retry": {"monthly": {"targets": [101], "attempts": 0}}}
    )
    monkeypatch.setattr(digest, "monthly_payload", AsyncMock(side_effect=OSError("диск полон")))

    for attempt in range(1, digest.RETRY_ATTEMPTS + 1):
        assert await digest.retry_undelivered_issues(SimpleNamespace()) == 0
        assert stored.value["retry"]["monthly"]["attempts"] == attempt
    await digest.retry_undelivered_issues(SimpleNamespace())
    assert "monthly" not in stored.value["retry"], "исчерпанная досылка не снята"
