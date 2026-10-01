"""Мини-сводки по алертам.

Правило слотов — чистая функция due_slot; содержимое — поведенческий
тест с базой: закрытый алерт зачёркнут и несёт рабочее время первым,
закрытый пределом НЕ зачёркнут, ждущий помечен ⏳.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import (
    AlertLog,
    BotUser,
    BusinessSide,
    Chat,
    ChatState,
    Interaction,
    InteractionState,
    Message,
    Staff,
    TransportActorKind,
)
from app.services import alert_digest
from app.services.alert_digest import (
    alert_digest_payload,
    claim_alert_digest,
    due_slot,
)
from app.services.staff import normalize_name
from tests.conftest import requires_db

CFG_CAL = {
    "weekdays": [1, 2, 3, 4, 5],
    "start": "10:00",
    "end": "19:00",
    "timezone": "Europe/Moscow",
    "holidays": ["2026-08-25"],
}

# Понедельник 24.08.2026, МСК = UTC+3.
MON_0900 = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
MON_1100 = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
MON_1500 = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
MON_1900 = datetime(2026, 8, 24, 16, 0, tzinfo=timezone.utc)
SAT_1900 = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)

# Времена сводок в тестах задаются явно: умолчание — настройка, а тест
# про правило слотов, а не про её сегодняшнее значение.
TIMES = ["10:35", "14:30", "18:30"]



def test_due_slot_matrix():
    """Слот — само время: времена настраиваются, дедуп по ним."""
    assert due_slot(MON_0900, CFG_CAL, {}, TIMES) == (None, []), "до 10:35 рано"
    assert due_slot(MON_1100, CFG_CAL, {}, TIMES) == ("10:35", ["10:35"])
    assert due_slot(SAT_1900, CFG_CAL, {}, TIMES) == (None, []), "суббота молчит"

    # Воркер, перезапущенный вечером, шлёт ОДНУ сводку — последнюю,
    # а не три подряд; но гасятся все просроченные слоты.
    slot, marks = due_slot(MON_1900, CFG_CAL, {}, TIMES)
    assert slot == "18:30"
    assert marks == ["10:35", "14:30", "18:30"]

    # Отработанный слот не повторяется, следующий — приходит в срок.
    today = "2026-08-24"
    assert due_slot(MON_1100, CFG_CAL, {"10:35": today}, TIMES) == (None, [])
    assert due_slot(MON_1500, CFG_CAL, {"10:35": today}, TIMES) == ("14:30", ["14:30"])


def test_slot_times_are_configurable():
    """Число сводок в день и время каждой — из настройки."""
    from app.services.alert_digest import slot_times

    times = slot_times({"alerts_digest_times": ["09:00", "17:00"]})
    assert times == ["09:00", "17:00"]
    assert due_slot(MON_1100, CFG_CAL, {}, times) == ("09:00", ["09:00"])
    # 15:00 по МСК — второго слота ещё нет, первый уже отработал.
    assert due_slot(MON_1500, CFG_CAL, {"09:00": "2026-08-24"}, times) == (None, [])
    assert due_slot(MON_1900, CFG_CAL, {}, times) == ("17:00", ["09:00", "17:00"])

    # Мусор мимо интерфейса не должен оставлять рассылку без времён вовсе.
    assert slot_times({"alerts_digest_times": ["ой"]}) == ["10:10", "14:30", "18:30"]
    assert slot_times({}) == ["10:10", "14:30", "18:30"], "умолчание — сразу после открытия дня"
    assert slot_times({"alerts_digest_times": "14:30, 10:35"}) == ["10:35", "14:30"]


def test_legacy_slot_marks_are_understood():
    """Слот, отработавший под старым именем, не уходит дважды."""
    today = "2026-08-24"
    assert due_slot(MON_1100, CFG_CAL, {"morning": today}, TIMES) == (None, [])
    slot, _ = due_slot(MON_1500, CFG_CAL, {"morning": today, "midday": today}, TIMES)
    assert slot is None


@requires_db
async def test_changing_times_does_not_fire_a_digest_at_once(session, monkeypatch):
    """Смена расписания — не просьба прислать сводку сейчас."""
    from app.bot.handlers.settings_ui import _after_change
    from app.services.settings_store import set_value

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return MON_1500  # 15:00 МСК: 10:10 и 14:30 уже прошли

    await _calendar(session)
    await _enable(session)
    monkeypatch.setattr(alert_digest, "datetime", _Frozen)

    # Включение гасит прошедшие сегодня слоты…
    await _after_change(session, "digest", "alerts_digest_enabled", True)
    await session.flush()
    assert await claim_alert_digest(session) is None

    # …и смена времён тоже: добавленное «14:45» не выстреливает немедленно.
    await set_value(
        session, "digest", "alerts_digest_times", ["10:35", "14:45"], actor_id=None
    )
    await _after_change(session, "digest", "alerts_digest_times", ["10:35", "14:45"])
    await session.flush()
    assert await claim_alert_digest(session) is None, "расписание — со следующего срока"


@requires_db
async def test_claim_is_once_per_slot(session, monkeypatch):
    class _Frozen:
        @staticmethod
        def now(tz=None):
            return MON_1100

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)
    await _calendar(session)

    assert await claim_alert_digest(session) is None, "рассылка выключена — тишина"
    await _enable(session)
    claimed = await claim_alert_digest(session)
    assert claimed is not None and claimed[0] == "10:10"
    assert await claim_alert_digest(session) is None, "второй тик того же слота"


# Календарь фикстуры должен действовать и для августовских данных теста:
# set_value заводит версию графика «с этой минуты», поэтому момент
# вступления в силу задаётся явно и заведомо раньше — иначе праздник
# в CFG_CAL не применится к тестовым обращениям.
CAL_SINCE = datetime(2026, 1, 1, tzinfo=timezone.utc)


async def _calendar(session):
    from app.services.settings_store import set_value

    for key, value in CFG_CAL.items():
        await set_value(
            session, "work_calendar", key, value, actor_id=None, now=CAL_SINCE
        )
    await session.flush()


async def _enable(session):
    from app.services.settings_store import set_value

    await set_value(session, "digest", "alerts_digest_enabled", True, actor_id=None)
    await session.flush()


def _owner(tg_user_id: int, **extra):
    from app.db.models import BotRole, BotUser, BotUserState

    return BotUser(
        tg_user_id=tg_user_id,
        role=BotRole.OWNER,
        permissions={},
        state=BotUserState.ACTIVE,
        **extra,
    )


async def _alert_case(
    session,
    tg_chat_id: int,
    title: str,
    sent_at: datetime,
    kind: str = "no_reaction",
    *,
    struck_at: datetime | None = None,
    with_interaction: bool = True,
    **inter,
):
    """Чат, обращение и запись журнала.

    `struck_at` — когда закрытие ЗАМЕТИЛИ (его ставит `strike_closed_alerts`);
    по нему сводка решает, в чей выпуск попадёт «ответа не требовалось»
    и «данных больше нет». `with_interaction=False` — обращения по этому
    сообщению больше не собирается, то есть исход `gone`.
    """
    from app.services.tracking import open_period

    chat = Chat(tg_chat_id=tg_chat_id, title=title, state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    opened_at = inter.pop("opened_at")
    # Открытый интервал наблюдения обязателен: кейс из чата на паузе
    # в блоке «ждут ответа» не показывается — пауза значит паузу.
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
    if with_interaction:
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
            kind=kind,
            recipients=[1000000001],
            sent_at=sent_at,
            delivered=True,
            struck_at=struck_at,
        )
    )
    await session.flush()


@requires_db
async def test_payload_marks_outcomes(session, monkeypatch):
    """Закрыт — зачёркнут, рабочее время первым; предел — не зачёркнут."""

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return MON_1500

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)

    # Адресаты — как у остальных рассылок: лички по праву алертов с учётом
    # флага «в личку». Выключивший личку не получает и сводку.
    session.add_all(
        [
            _owner(1000000001, display_name="Владелец (тест)"),
            _owner(100200300, notify_personal=False),
        ]
    )
    await _calendar(session)
    await _enable(session)

    irina = Staff(
        full_name="Ирина Соколова", normalized_name=normalize_name("Ирина Соколова")
    )
    session.add(irina)
    await session.flush()

    opened = MON_1500 - timedelta(hours=3)
    # Закрытый: реакция через 42 календарных минуты.
    await _alert_case(
        session,
        -100950001,
        "Сигма",
        # Алерт уходит по сорванному порогу — то есть ПОСЛЕ обращения
        # и ДО ответа: иначе строка «алерт 13:00 · закрыт 12:42» читалась бы
        # как закрытие раньше алерта.
        sent_at=opened + timedelta(minutes=31),
        opened_at=opened,
        state=InteractionState.ANSWERED,
        first_reaction_at=opened + timedelta(minutes=42),
        first_reaction_staff_id=irina.id,
        ttfr_seconds=42 * 60,
        ttfr_business_seconds=40 * 60,
    )
    # Закрытый пределом: ответа не было. Открыт в пятницу — окно суток
    # после срока истекло в субботу, то есть смерть попадает в окно
    # «с прошлой сводки» (пятничный вечерний выпуск).
    await _alert_case(
        session,
        -100950002,
        "Северный ветер",
        sent_at=MON_1500 - timedelta(days=3),
        opened_at=MON_1500 - timedelta(days=3),
        state=InteractionState.ABANDONED,
    )
    # Всё ещё ждёт.
    await _alert_case(
        session,
        -100950003,
        "ООО Вектор",
        sent_at=MON_1500 - timedelta(hours=1),
        opened_at=opened,
        state=InteractionState.OPEN,
    )

    # Открытый кейс, снятый руководителем: «ответ не требуется».
    from app.db.models import BreachDismissal

    owner = await session.scalar(select(BotUser).where(BotUser.tg_user_id == 1000000001))
    await session.flush()
    await _alert_case(
        session,
        -100950004,
        "Ромашка",
        sent_at=MON_1500 - timedelta(hours=1),
        opened_at=opened,
        state=InteractionState.OPEN,
    )
    romashka = await session.scalar(select(Chat).where(Chat.tg_chat_id == -100950004))
    romashka_msg = await session.scalar(select(Message).where(Message.chat_id == romashka.id))
    session.add(
        BreachDismissal(
            chat_id=romashka.id,
            opened_by_message_id=romashka_msg.id,
            dismissed_by=owner.id,
            # Момент решения задаётся явно: он же решает, в какой выпуск
            # попадёт строка (снятие показывается один раз).
            dismissed_at=MON_1500 - timedelta(minutes=30),
        )
    )
    await session.flush()

    # «Прошлая сводка» — вечер пятницы: с тех пор и показываем закрытое.
    payload = await alert_digest_payload(
        session, "midday", MON_1500 - timedelta(days=3, hours=1)
    )
    assert payload is not None
    view, recipients = payload
    text = view.text
    assert recipients == [1000000001]

    # Блоки: горящее сверху и отдельно от закрытого.
    assert "🔥 <b>ЖДУТ ОТВЕТА</b>" not in text, (
        "общая шапка убрана: при двух говорящих заголовках она лишний этаж"
    )
    assert "🔴 <b>Нет реакции менеджера</b>" in text, "ступень названа заголовком блока"
    assert "❓ <b>Остались без ответа</b>" in text
    assert "🟢 <b>Закрыто</b>" in text, (
        "заголовок блока — кружком: галочка стоит у каждой строки, "
        "и в шапке она читалась дублем"
    )
    assert text.index("Нет реакции менеджера") < text.index("Закрыто"), "горящее — первым"

    # Знак исхода стоит ПЕРЕД названием чата: глаз ищет маркер там,
    # где пункт начинается.
    assert "✅ <s>«Сигма»" in text, "закрытый алерт зачёркнут, знак впереди"
    assert "Ирина Соколова" in text
    assert "за 40 мин рабочего времени (календарных 42 мин)" in text, (
        "рабочее время первым, календарное в скобках"
    )
    assert "<s>«Северный ветер»" not in text, "закрытое пределом НЕ зачёркивается"
    assert "❓ «Северный ветер»" in text, "у оставшегося без ответа знак тоже впереди"
    assert "закрыто без ответа" in text
    assert "🔥 «ООО Вектор»" in text, (
        "ждущий алерт — горящая просрочка, не штатное ожидание, "
        "и огонёк открывает пункт"
    )
    assert "без ответа уже" in text
    assert "✋ <s>«Ромашка»" in text, "снятое вручную закрыто своим знаком впереди"
    assert "ответ не требуется (закрыто вручную: Владелец (тест))" in text, (
        "кто закрыл — в той же строке"
    )
    assert "Ждут ответа: 1 · без ответа: 1 · закрыто: 1 · снято решением: 1" in text

    # Приписка под строкой: когда и почему кейс закрылся.
    assert "алерт ушёл" not in text, "старая формулировка убрана"
    assert (
        "алерт сработал в 12:31, 24.08 · закрыт в 12:42, 24.08 — менеджер отработал"
        in text
    ), "у отработанного кейса — время закрытия и причина; время впереди даты"
    assert "закрыт в 14:30, 24.08 — закрыто вручную" in text
    assert text.count("Владелец (тест)") == 1, (
        "имя снявшего стоит в строке — в приписке его не повторяем"
    )
    assert "закрыто принудительно: перестали ждать" in text, (
        "истёкшее окно ожидания — принудительное закрытие, а не отработка"
    )
    assert "закрыт —" not in text, "у висящего кейса закрытия ещё нет"

    # Кнопки — у того, с чем можно что-то сделать: висящее и оставшееся
    # без ответа. Под закрытым кнопка не нужна — там всё уже случилось.
    assert len(view.buttons) == 2
    assert all(cb.startswith("al:ctxd:") for _, cb in view.buttons), view.buttons
    assert view.buttons[0][0].startswith("💬 1. ООО Вектор"), "первым — горящий кейс"


@requires_db
async def test_old_hanging_case_stays_visible(session, monkeypatch):
    """Висящее показывается любой давности, закрытое — только за окно."""

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return MON_1500

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)

    long_ago = MON_1500 - timedelta(days=5)
    await _alert_case(
        session,
        -100960001,
        "Давний",
        sent_at=long_ago,
        opened_at=long_ago,
        state=InteractionState.OPEN,
    )
    await _alert_case(
        session,
        -100960002,
        "Давно закрытый",
        sent_at=long_ago,
        opened_at=long_ago,
        state=InteractionState.ANSWERED,
        first_reaction_at=long_ago + timedelta(minutes=42),
        ttfr_seconds=42 * 60,
        ttfr_business_seconds=40 * 60,
    )

    payload = await alert_digest_payload(session, "14:30")
    assert payload is not None
    text = payload[0].text

    assert "«Давний»" in text, "висящий кейс обязан быть виден, сколько бы ни висел"
    assert "«Давно закрытый»" not in text, "закрытое пятидневной давности — не новость"
    assert "Ждут ответа: 1" in text


@requires_db
async def test_dead_specialist_case_is_shown_after_its_window(session, monkeypatch):
    """Кейс, которого перестали ждать, обязан показаться — хотя бы раз.

    Алерт специалиста уходит в момент просрочки, а ждать перестают ровно
    через 7 дней после неё. Фильтр блока «остались без ответа» по возрасту
    АЛЕРТА убрал бы кейс из сводки в ту же минуту, когда его туда положили,
    поэтому отсчёт идёт от момента смерти кейса.
    """

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return MON_1500

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)

    # Передали специалисту 8 дней назад, срок — назавтра, окно — неделя:
    # ждать перестали вчера, алерту при этом уже больше недели.
    handoff = MON_1500 - timedelta(days=9, hours=2)
    await _alert_case(
        session,
        -100990001,
        "Забытый специалистом",
        sent_at=handoff + timedelta(days=1),
        opened_at=handoff - timedelta(minutes=5),
        state=InteractionState.ABANDONED,
        first_reaction_at=handoff,
        handoff_at=handoff,
        kind="no_substantive",
    )

    payload = await alert_digest_payload(session, "14:30")
    assert payload is not None
    text = payload[0].text

    assert "«Забытый специалистом»" in text, (
        "кейс умер вчера, а из сводки пропал бы вместе со своим алертом"
    )
    assert "❓ <b>Остались без ответа</b>" in text


@requires_db
async def test_false_alert_does_not_burn_forever(session, monkeypatch):
    """Обращение, закрытое как «вопроса не было», перестаёт гореть.

    «Висит» — про СОСТОЯНИЕ обращения, а не про «ответа не видно».
    """

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return MON_1500

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)

    await _alert_case(
        session,
        -100980001,
        "Уведомление",
        sent_at=MON_1500 - timedelta(hours=2),
        opened_at=MON_1500 - timedelta(hours=3),
        state=InteractionState.NO_RESPONSE_NEEDED,
    )

    payload = await alert_digest_payload(session, "14:30")
    assert payload is not None
    text = payload[0].text

    assert "🔥 " not in text, "ложный алерт не должен гореть"
    assert "🤖 <s>«" in text, "знак исхода — перед названием чата"
    assert "ответа не требовалось: вопроса не было" in text
    assert "Ждут ответа: 0" in text
    assert "ответа не требовалось: 1" in text


@requires_db
async def test_case_closed_in_the_window_is_shown_even_with_an_old_alert(
    session, monkeypatch
):
    """Алерт ушёл давно, ответили вчера — сводка обязана сказать «закрыто».

    Блок отбирается по моменту закрытия, а не по дате алерта: иначе кейс
    в день ответа исчезал бы и из «ждут», и из «закрыто».
    """

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return MON_1500

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)

    long_ago = MON_1500 - timedelta(days=4)
    answered = MON_1500 - timedelta(hours=20)  # вчера, внутри окна выпуска
    await _alert_case(
        session,
        -100991001,
        "Ответили поздно",
        sent_at=long_ago,
        opened_at=long_ago,
        state=InteractionState.ANSWERED,
        first_reaction_at=answered,
        ttfr_seconds=int((answered - long_ago).total_seconds()),
        ttfr_business_seconds=9 * 3600,
    )

    payload = await alert_digest_payload(session, "14:30")
    assert payload is not None, "закрытый вчера кейс не попал в сводку вовсе"
    text = payload[0].text
    assert "«Ответили поздно»" in text
    assert "🟢 <b>Закрыто</b>" in text and "закрыто: 1" in text


@requires_db
async def test_closed_case_is_shown_once(session, monkeypatch):
    """Закрытие показывается ровно один раз; горящие висят, пока не закроются."""

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return MON_1500

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)

    opened = MON_1500 - timedelta(hours=4)
    await _alert_case(
        session,
        -100992001,
        "Закрыт до обеда",
        sent_at=opened + timedelta(minutes=31),
        opened_at=opened,
        state=InteractionState.ANSWERED,
        first_reaction_at=opened + timedelta(hours=1),
        ttfr_seconds=3600,
        ttfr_business_seconds=3600,
    )
    await _alert_case(
        session,
        -100992002,
        "Всё ещё горит",
        sent_at=opened + timedelta(minutes=31),
        opened_at=opened,
        state=InteractionState.OPEN,
    )

    # Сводка сразу после закрытия — показывает и горящее, и закрытое.
    first = await alert_digest_payload(
        session, "14:30", MON_1500 - timedelta(hours=5)
    )
    assert first is not None
    assert "«Закрыт до обеда»" in first[0].text
    assert "«Всё ещё горит»" in first[0].text

    # Следующая сводка: закрытое уже рассказано, горящее — по-прежнему горит.
    second = await alert_digest_payload(session, "18:30", MON_1500 - timedelta(minutes=5))
    assert second is not None
    assert "«Закрыт до обеда»" not in second[0].text, (
        "закрытый алерт повторился в следующей сводке"
    )
    assert "«Всё ещё горит»" in second[0].text, "горящее обязано висеть до закрытия"
    assert "Ждут ответа: 1" in second[0].text


@requires_db
async def test_paused_chat_stops_hanging(session, monkeypatch):
    """Чат сняли с анализа — его кейс перестаёт гореть в сводке."""

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return MON_1500

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)

    await _alert_case(
        session,
        -100970001,
        "На паузе",
        sent_at=MON_1500 - timedelta(hours=1),
        opened_at=MON_1500 - timedelta(hours=3),
        state=InteractionState.OPEN,
    )
    from app.services.tracking import close_period

    chat = await session.scalar(select(Chat).where(Chat.tg_chat_id == -100970001))
    chat.state = ChatState.PAUSED
    await close_period(session, chat, reason="test", at=MON_1500)
    await session.flush()

    assert await alert_digest_payload(session, "14:30") is None


@requires_db
async def test_empty_payload_requires_scheduled_opt_in(session):
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)

    assert await alert_digest_payload(session, "morning") is None


@requires_db
@pytest.mark.parametrize("times", [
    ["13:00"],
    ["10:10", "13:00", "16:40"],
    ["10:10", "12:00", "14:00", "16:40"],
])
async def test_every_scheduled_empty_digest_arrives_once(session, monkeypatch, times):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    import app.db.base as db_base
    from app.services.settings_store import set_value

    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)
    await set_value(session, "digest", "alerts_digest_times", times)
    await session.flush()

    class Frozen(datetime):
        moment = MON_0900

        @classmethod
        def now(cls, tz=None):
            return cls.moment.astimezone(tz) if tz else cls.moment

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(alert_digest, "datetime", Frozen)
    monkeypatch.setattr(db_base, "session_scope", fake_scope)
    bot = AsyncMock()
    # До первого настроенного времени выпуска нет.
    assert not await alert_digest.maybe_send_alert_digest(bot)
    bot.send_message.assert_not_awaited()
    for slot in times:
        hour, minute = map(int, slot.split(":"))
        Frozen.moment = MON_0900 + timedelta(hours=hour - 9, minutes=minute + 1)
        bot = AsyncMock()
        assert await alert_digest.maybe_send_alert_digest(bot)
        bot.send_message.assert_awaited_once()
        sent = bot.send_message.call_args.args[1]
        assert alert_digest.slot_title(slot, times) in sent
        assert "Алертов в ожидании ответа нет" in sent
        assert "24.08" in sent
        # Повторный тик/новый объект бота не отправляет тот же выпуск.
        restarted_bot = AsyncMock()
        assert not await alert_digest.maybe_send_alert_digest(restarted_bot)
        restarted_bot.send_message.assert_not_awaited()
    # После последнего выпуска не появляется дополнительный вечерний слот.
    Frozen.moment = MON_1900 + timedelta(minutes=5)
    assert not await alert_digest.maybe_send_alert_digest(restarted_bot)
    restarted_bot.send_message.assert_not_awaited()


@requires_db
async def test_empty_digest_does_not_repeat_previous_closed_case(session, monkeypatch):
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)
    await _alert_case(
        session, -100970021, "Уже показанный кейс",
        sent_at=MON_1100 + timedelta(minutes=31), opened_at=MON_1100,
        state=InteractionState.ANSWERED,
        first_reaction_at=MON_1100 + timedelta(minutes=42),
    )

    class Frozen(datetime):
        moment = MON_1500

        @classmethod
        def now(cls, tz=None):
            return cls.moment.astimezone(tz) if tz else cls.moment

    monkeypatch.setattr(alert_digest, "datetime", Frozen)
    earlier = await alert_digest_payload(session, "14:30", MON_1100, allow_empty=True)
    assert "Уже показанный кейс" in earlier[0].text
    Frozen.moment = MON_1900
    final = await alert_digest_payload(session, "18:30", MON_1500, allow_empty=True)
    assert final is not None
    assert "Уже показанный кейс" not in final[0].text
    assert "новых итогов по алертам нет" in final[0].text
    assert not final[0].buttons


@requires_db
async def test_empty_digest_still_requires_recipients(session):
    await _calendar(session)
    await _enable(session)
    assert await alert_digest_payload(session, "18:30", allow_empty=True) is None


@requires_db
async def test_manual_digest_screen(session, monkeypatch):
    """Кнопка «Сводка по алертам (сейчас)»: экран рисуется и на пустом окне."""
    import app.bot.handlers.reports_lab as lab
    from contextlib import asynccontextmanager

    from app.bot.callbacks import LabAction
    from app.db.models import BotRole, BotUser, BotUserState

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(lab, "session_scope", fake_scope)
    await _calendar(session)

    class _FakeMessage:
        def __init__(self) -> None:
            self.text = None
            self.markup = None

        async def edit_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
            self.text = text
            self.markup = reply_markup

    class _FakeQuery:
        def __init__(self) -> None:
            self.message = _FakeMessage()

        async def answer(self, *args, **kwargs):
            return None

    owner = BotUser(
        tg_user_id=770300,
        role=BotRole.OWNER,
        permissions={},
        state=BotUserState.ACTIVE,
    )
    query = _FakeQuery()
    await lab.on_alert_digest(query, owner)

    assert query.message.text is not None
    assert "алерт" in query.message.text.lower()
    callbacks = [
        b.callback_data
        for row in query.message.markup.inline_keyboard
        for b in row
        if b.callback_data
    ]
    assert LabAction(kind=lab.ALERT_DIGEST_KIND).pack() in callbacks, (
        "у экрана обязана быть кнопка «Обновить»"
    )


def test_slot_titles_are_plain_words():
    """Заголовок — простыми словами, без времени выпуска."""
    from app.services.alert_digest import MANUAL_SLOT, slot_title

    times = ["10:10", "13:00", "16:40"]
    assert slot_title("10:10", times) == "Утренняя сводка по алертам"
    assert slot_title("13:00", times) == "Дневная сводка по алертам"
    assert slot_title("16:40", times) == "Вечерняя сводка по алертам"
    assert slot_title(MANUAL_SLOT, times) == "Сводка по алертам (по запросу)"
    # Одна сводка в день — не «утренняя» и не «вечерняя», просто сводка.
    assert slot_title("13:00", ["13:00"]) == "Сводка по алертам"
    for title in (slot_title(slot, times) for slot in times):
        assert ":" not in title, f"время выпуска не должно попадать в заголовок: {title}"


# ── Позднее закрытие: момент замеченного закрытия, а не время алерта ───


@requires_db
async def test_a_late_no_need_closure_is_shown_once_after_it_happened(
    session, monkeypatch
):
    """Кейс закрылся вердиктом через два часа — и показан ровно один раз.

    У «ответа не требовалось» своей отметки нет, поэтому момент закрытия —
    `struck_at`, а не время алерта: иначе закрытие, случившееся после
    очередной сводки, не попало бы ни в одну сводку и в счётчик шапки.
    """
    moment = {"now": MON_1100}

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return moment["now"]

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)

    opened = MON_1100 - timedelta(hours=1)
    await _alert_case(
        session,
        -100993001,
        "Отозвали просьбу",
        sent_at=opened + timedelta(minutes=31),
        opened_at=opened,
        state=InteractionState.OPEN,
    )

    # Утренняя сводка: кейс ещё горит.
    first = await alert_digest_payload(session, "10:35", MON_1100 - timedelta(hours=2))
    assert first is not None and "«Отозвали просьбу»" in first[0].text
    assert "Ждут ответа: 1" in first[0].text

    # Через два часа клиент отозвал просьбу: обращение закрылось вердиктом,
    # и тем же тиком воркер отметил закрытие (`strike_closed_alerts`).
    closed_at = MON_1100 + timedelta(hours=2)
    entry = await session.scalar(select(AlertLog))
    interaction = await session.scalar(select(Interaction))
    interaction.state = InteractionState.NO_RESPONSE_NEEDED
    entry.struck_at = closed_at
    await session.flush()

    moment["now"] = MON_1500
    second = await alert_digest_payload(session, "14:30", MON_1100)
    assert second is not None, "закрытие выпало из сводок вовсе"
    text = second[0].text
    assert "«Отозвали просьбу»" in text, (
        "закрытие между выпусками не показано ни в одной сводке"
    )
    assert "🤖 <s>«" in text and "ответа не требовалось: 1" in text
    assert "Ждут ответа: 0" in text

    # И ровно один раз: следующий выпуск про это уже рассказал.
    moment["now"] = MON_1900
    third = await alert_digest_payload(session, "18:30", MON_1500)
    assert third is None or "«Отозвали просьбу»" not in third[0].text, (
        "закрытие показано дважды"
    )


@requires_db
async def test_a_late_gone_closure_is_shown_once_after_it_happened(
    session, monkeypatch
):
    """То же для «данных об обращении больше нет».

    Обращение пересобралось иначе (вердикт изменился), и своей отметки
    времени у исхода нет. Момент — тот же `struck_at`: его ставит ветка
    «обращения больше нет» в `strike_closed_alerts` тем тиком, когда
    обращение перестало собираться.
    """
    moment = {"now": MON_1100}

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return moment["now"]

    monkeypatch.setattr(alert_digest, "datetime", _Frozen)
    session.add(_owner(1000000001))
    await _calendar(session)
    await _enable(session)

    opened = MON_1100 - timedelta(hours=1)
    await _alert_case(
        session,
        -100993002,
        "Эпизод слился",
        sent_at=opened + timedelta(minutes=31),
        opened_at=opened,
        with_interaction=False,
        struck_at=MON_1100 + timedelta(hours=2),
    )

    # Утренняя сводка: закрытие ещё не замечено — рассказывать нечего.
    first = await alert_digest_payload(session, "10:35", MON_1100 - timedelta(hours=2))
    assert first is None or "«Эпизод слился»" not in first[0].text

    moment["now"] = MON_1500
    second = await alert_digest_payload(session, "14:30", MON_1100)
    assert second is not None and "«Эпизод слился»" in second[0].text, (
        "исход «данных больше нет» выпал из всех сводок"
    )

    moment["now"] = MON_1900
    third = await alert_digest_payload(session, "18:30", MON_1500)
    assert third is None or "«Эпизод слился»" not in third[0].text, (
        "закрытие показано дважды"
    )
