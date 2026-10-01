"""Очередь классификации переживает отказ провайдера за минуты, а не за час.

Причинная очередь (`worker.causal_pending_condition`) держит за отказавшим
сообщением весь его чат, поэтому повтор должен быть быстрым и конечным.

1. Срок повтора считается минутами (5, затем 15), и его задаёт не
   удаление строки, а условие очереди — одно для воркера и для экрана.
2. Попыток ровно `MAX_CLASSIFY_ATTEMPTS`; последняя идёт на РЕЗЕРВНУЮ
   модель именно для этого сообщения и общий размыкатель не трогает.
3. Исчерпав попытки, воркер пишет ТЕХНИЧЕСКИЙ вердикт: очередь считает
   сообщение разрешённым и идёт дальше, а движок эпизодов такую строку
   не читает — ни реакции, ни обращения по ней не возникает.
4. Успех стирает историю отказов: следующая помеха сети начинает счёт
   попыток заново.
5. Правленое сообщение живёт по ТЕМ ЖЕ правилам: сброс производного
   делается один раз на правку, счёт попыток в текущем цикле разметки
   переживает тик, а исчерпание гасит флаг правки.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.services.ai import PROMPT_PROFILES, AiClient, FallbackCircuit
from app.services.ai_stats import (
    MAX_CLASSIFY_ATTEMPTS,
    RETRY_BACKOFF_MINUTES,
    pending_conditions,
)
from app.services.verdicts import SOURCE_MODEL, SOURCE_RULE, SOURCE_TECHNICAL
from tests.conftest import requires_db

PRIMARY = "openai/gpt-oss-120b"
FALLBACK = "deepseek-ai/DeepSeek-V4-Pro"
ACCEPTED = [PRIMARY, FALLBACK]

CLIENT_VERDICT = {"label": "request", "requires_response": True}


# ── Обвязка: подставной провайдер (тот же приём, что в test_ai_fallback) ──


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def text(self) -> str:
        return self._body


class _Http:
    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.sent: list[dict] = []

    def post(self, url, headers=None, json=None):  # noqa: A002 — сигнатура aiohttp
        self.sent.append(json)
        answer = self.answers[json["model"]]
        return answer() if callable(answer) else answer


def _ok(verdict: dict) -> _Response:
    return _Response(
        200,
        json.dumps(
            {
                "choices": [{"message": {"content": json.dumps(verdict)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ),
    )


def _timeout() -> _Response:
    raise TimeoutError()


@pytest.fixture
def enabled(monkeypatch):
    """Основная модель и резервная настроены."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_model", PRIMARY, raising=False)
    monkeypatch.setattr(settings, "ai_prompt_profile", "", raising=False)
    monkeypatch.setattr(settings, "ai_fallback_model", FALLBACK, raising=False)
    monkeypatch.setattr(settings, "ai_fallback_prompt_profile", "", raising=False)
    monkeypatch.setattr(settings, "ai_fallback_retries", 3, raising=False)
    monkeypatch.setattr(settings, "ai_fallback_retry_window_seconds", 300, raising=False)
    monkeypatch.setattr(settings, "ai_monthly_token_limit", None, raising=False)
    return settings


def _client(http: _Http, *, circuit: FallbackCircuit | None = None) -> AiClient:
    client = AiClient(circuit=circuit or FallbackCircuit())
    client._http = http
    return client


# ── 1. Срок повтора — минуты, и его задаёт условие очереди ─────────────


def test_retry_delay_is_minutes_and_bounded_by_attempts():
    """Кортеж задаёт и сроки, и число попыток: их нельзя разойтись."""
    assert RETRY_BACKOFF_MINUTES == (5, 15), "срок повтора вернулся к часу"
    assert MAX_CLASSIFY_ATTEMPTS == len(RETRY_BACKOFF_MINUTES) + 1 == 3
    # Худшее ожидание чата за одно сообщение — сумма сроков, а не час.
    assert sum(RETRY_BACKOFF_MINUTES) <= 30


def test_the_worker_never_deletes_error_rows_by_a_timer():
    """Строка отказа — это счётчик попыток; удалять её по таймеру нельзя."""
    from app.worker import main as worker

    source = inspect.getsource(worker.classify_pending)
    assert "timedelta(hours=1)" not in source, "часовое удаление отказов вернулось"
    assert "retry_before" not in source


def test_the_queue_ignores_the_prompt_version():
    """Очередь не зависит от версии промпта."""
    source = inspect.getsource(pending_conditions)
    assert "prompt_version ==" not in source.replace(
        "`prompt_version == PROMPT_VERSION`", ""
    )


def test_the_screen_and_the_worker_share_one_backoff():
    """Экран состояния обязан показывать ту же очередь, что возьмёт воркер."""
    from app.services import ai_stats
    from app.worker import main as worker

    assert "pending_conditions" in inspect.getsource(worker.classify_pending)
    assert "pending_conditions" in inspect.getsource(ai_stats.pending_count)
    # Момент времени — параметр, а не скрытый `now()`: иначе два запроса
    # одного тика могли бы разойтись на границе срока.
    assert "now" in inspect.signature(pending_conditions).parameters


# ── 2. Последняя попытка — резервная модель, без общего размыкателя ────


async def test_the_last_attempt_goes_to_the_fallback_model(enabled):
    """`force_fallback` шлёт резервную модель и её промпт, минуя основную."""
    http = _Http({PRIMARY: _ok(CLIENT_VERDICT), FALLBACK: _ok(CLIENT_VERDICT)})
    client = _client(http)

    detail = await client.classify_detailed(
        "СИСТЕМА ОСНОВНОЙ", "вход", is_client=True, force_fallback=True
    )

    assert [body["model"] for body in http.sent] == [FALLBACK], "основную всё же звали"
    profile = PROMPT_PROFILES["deepseek-ds1b"]
    assert http.sent[0]["messages"][0]["content"] == profile.client_system
    assert detail["fallback"] is True
    assert detail["model"] == FALLBACK
    assert detail["prompt_db_version"] == profile.db_version


async def test_a_forced_fallback_never_touches_the_shared_circuit(enabled):
    """Размыкатель — про здоровье провайдера, а не про одно сообщение.

    Отказ резерва на последней попытке не имеет права ни перевести всю
    классификацию на резерв, ни сбросить накопленные отказы основной.
    """
    circuit = FallbackCircuit()
    circuit.failures = [1.0, 2.0]
    http = _Http({PRIMARY: _ok(CLIENT_VERDICT), FALLBACK: _timeout})
    client = _client(http, circuit=circuit)

    detail = await client.classify_detailed(
        "СИСТЕМА ОСНОВНОЙ", "вход", is_client=True, force_fallback=True
    )

    assert detail["error"] is not None
    assert circuit.failures == [1.0, 2.0], "счётчик отказов основной сдвинулся"
    assert circuit.active is False, "одно сообщение увело на резерв всю разметку"


async def test_without_the_flag_the_primary_is_still_asked_first(enabled):
    """Флага нет — путь ровно прежний: сначала основная."""
    http = _Http({PRIMARY: _ok(CLIENT_VERDICT), FALLBACK: _ok(CLIENT_VERDICT)})
    client = _client(http)

    await client.classify_detailed("СИСТЕМА ОСНОВНОЙ", "вход", is_client=True)

    assert [body["model"] for body in http.sent] == [PRIMARY]


async def test_without_a_fallback_model_the_flag_is_a_no_op(monkeypatch):
    """Резерв не настроен — флаг ничего не меняет, зовём основную."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_model", PRIMARY, raising=False)
    monkeypatch.setattr(settings, "ai_fallback_model", "", raising=False)
    monkeypatch.setattr(settings, "ai_monthly_token_limit", None, raising=False)
    http = _Http({PRIMARY: _ok(CLIENT_VERDICT)})
    client = _client(http)

    await client.classify_detailed(
        "СИСТЕМА ОСНОВНОЙ", "вход", is_client=True, force_fallback=True
    )

    assert [body["model"] for body in http.sent] == [PRIMARY]


# ── 3. Таймаут клиента — настройка, а не константа ─────────────────────


def test_the_client_timeout_comes_from_the_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_monthly_token_limit", None, raising=False)
    monkeypatch.setattr(settings, "ai_timeout_seconds", 45, raising=False)
    assert AiClient().timeout_seconds == 45


def test_the_default_timeout_fits_under_the_worker_watchdog(monkeypatch):
    """Худший проход обязан оставаться короче предела сторожа живости."""
    from app.config import Settings
    from app.health import MAX_AGE_SECONDS
    from app.worker.main import CLASSIFY_DEADLINE_SECONDS

    monkeypatch.delenv("AI_TIMEOUT_SECONDS", raising=False)
    default_timeout = Settings(_env_file=None).ai_timeout_seconds
    worst_pass = CLASSIFY_DEADLINE_SECONDS + default_timeout
    assert worst_pass < MAX_AGE_SECONDS["worker"]


# ── 4. Технический вердикт: движок его не читает ──────────────────────


def test_the_engine_ignores_technical_verdicts():
    """Метка None не имеет права стать вердиктом «ничего не требуется»."""
    from app.services import episodes

    source = inspect.getsource(episodes)
    assert source.count("SOURCE_TECHNICAL") >= 3, (
        "движок читает технические строки — сообщение получит ложный вердикт"
    )
    assert SOURCE_TECHNICAL not in {SOURCE_MODEL, SOURCE_RULE}


def test_an_exhausted_message_does_not_block_its_chat():
    """Ради этого технический вердикт и пишется."""
    from app.worker import main as worker

    source = inspect.getsource(worker.classify_pending)
    assert "and not exhausted" in source, "исчерпанное сообщение снова держит чат"
    assert "classify.gave_up" in source, "пропуск не помечен для ручной проверки"


# ── 5. То же самое на базе: условие очереди целиком ───────────────────


async def _chat_with_message(
    session, *, chat_id: int, message_id: int, sent_at: datetime, client: bool = False
):
    """Чат под наблюдением и одно сообщение в нём.

    `client=True` — сообщение клиента: у правки клиентского сообщения
    атрибуция не пересчитывается, и тест проверяет ровно очередь.
    """
    from app.db.models import (
        BusinessSide,
        Chat,
        ChatState,
        ChatTrackingPeriod,
        Message,
        TransportActorKind,
    )

    session.add(Chat(id=chat_id, tg_chat_id=chat_id, title="т", state=ChatState.TRACKED))
    # Причинная очередь смотрит «сообщение пришло под наблюдением»
    # (`tracking.observed_filter`), а это ОТДЕЛЬНАЯ таблица интервалов:
    # без открытого интервала чат для неё невидим, и тест проверял бы
    # не то условие.
    session.add(
        ChatTrackingPeriod(chat_id=chat_id, started_at=sent_at - timedelta(days=30))
    )
    session.add(
        Message(
            id=message_id,
            chat_id=chat_id,
            tg_message_id=message_id,
            sent_at=sent_at,
            text="ПП в банке",
            business_side=BusinessSide.CLIENT if client else BusinessSide.COMPANY,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
        )
    )
    await session.flush()


def _failure(message_id: int, created_at: datetime, attempts: int = 1):
    """Строка отказа. Она ОДНА на сообщение: счёт попыток — в `attempts`."""
    from app.db.models import Classification

    return Classification(
        message_id=message_id,
        model=PRIMARY,
        prompt_version=1502,
        source=SOURCE_MODEL,
        error="TimeoutError: ",
        attempts=attempts,
        created_at=created_at,
    )


def _technical(message_id: int):
    from app.db.models import Classification

    return Classification(
        message_id=message_id,
        model=PRIMARY,
        prompt_version=0,
        source=SOURCE_TECHNICAL,
        error=None,
    )


async def _pending_ids(session, now: datetime) -> list[int]:
    from sqlalchemy import select

    from app.db.models import Message

    rows = await session.scalars(
        select(Message.id).where(*pending_conditions(ACCEPTED, now=now))
    )
    return list(rows)


@requires_db
async def test_a_second_failure_row_is_impossible_which_is_why_attempts_exist(session):
    """Уникальность включает модель и версию: строка отказа физически одна.

    Этот тест держит ПРИЧИНУ правки: считать попытки строками нельзя,
    и «просто перестать удалять строку» вместо удаления по таймеру
    не работает — вторая строка не вставится.
    """
    from sqlalchemy.exc import IntegrityError

    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9000, message_id=90000, sent_at=now - timedelta(hours=2)
    )
    session.add(_failure(90000, now - timedelta(minutes=30)))
    await session.flush()
    session.add(_failure(90000, now - timedelta(minutes=10)))
    with pytest.raises(IntegrityError):
        await session.flush()


@requires_db
async def test_one_failure_returns_to_the_queue_after_five_minutes(session):
    """Не через час: чат стоит за отказавшим сообщением все эти минуты."""
    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9001, message_id=90001, sent_at=now - timedelta(hours=2)
    )
    session.add(_failure(90001, now - timedelta(minutes=4)))
    await session.flush()

    assert await _pending_ids(session, now) == []
    assert await _pending_ids(session, now + timedelta(minutes=2)) == [90001]


@requires_db
async def test_the_second_failure_waits_fifteen_minutes(session):
    """Срок растёт, но остаётся минутным."""
    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9002, message_id=90002, sent_at=now - timedelta(hours=2)
    )
    session.add(_failure(90002, now - timedelta(minutes=10), attempts=2))
    await session.flush()

    assert await _pending_ids(session, now) == []
    assert await _pending_ids(session, now + timedelta(minutes=6)) == [90002]


@requires_db
async def test_after_the_last_attempt_the_message_never_returns(session):
    """Попытки исчерпаны — очередь его больше не берёт и не платит за него."""
    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9003, message_id=90003, sent_at=now - timedelta(hours=2)
    )
    session.add(
        _failure(90003, now - timedelta(minutes=20), attempts=MAX_CLASSIFY_ATTEMPTS)
    )
    await session.flush()

    assert await _pending_ids(session, now) == []
    assert await _pending_ids(session, now + timedelta(days=1)) == []


@requires_db
async def test_a_technical_verdict_resolves_the_message_for_the_queue(session):
    """Технический вердикт снимает сообщение с очереди — и только это."""
    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9004, message_id=90004, sent_at=now - timedelta(hours=2)
    )
    session.add(_failure(90004, now - timedelta(minutes=30)))
    await session.flush()
    assert await _pending_ids(session, now) == [90004]

    # Строка отказа ОСТАЁТСЯ рядом: она и есть след для ручной проверки.
    session.add(_technical(90004))
    await session.flush()

    assert await _pending_ids(session, now) == []


@requires_db
async def test_a_technical_verdict_unblocks_the_causal_queue(session):
    """Следующие сообщения чата перестают ждать — ровно цель правки."""
    from sqlalchemy import select

    from app.db.models import BusinessSide, Message, TransportActorKind
    from app.worker.main import causal_pending_condition

    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9005, message_id=90005, sent_at=now - timedelta(hours=2)
    )
    session.add(
        Message(
            id=90006,
            chat_id=9005,
            tg_message_id=90006,
            sent_at=now - timedelta(hours=1),
            text="а что со счётом?",
            business_side=BusinessSide.CLIENT,
            transport_actor_kind=TransportActorKind.HUMAN_USER,
        )
    )
    session.add(_failure(90005, now - timedelta(minutes=30)))
    await session.flush()

    blocked = list(
        await session.scalars(
            select(Message.id)
            .where(Message.chat_id == 9005)
            .where(causal_pending_condition(ACCEPTED))
        )
    )
    assert 90006 not in blocked, "сообщение за отказом должно ждать"

    session.add(_technical(90005))
    await session.flush()

    unblocked = list(
        await session.scalars(
            select(Message.id)
            .where(Message.chat_id == 9005)
            .where(causal_pending_condition(ACCEPTED))
        )
    )
    assert 90006 in unblocked, "чат остался за исчерпанным сообщением"


@requires_db
async def test_the_engine_does_not_read_a_technical_verdict(session):
    """Движок обязан видеть такое сообщение НЕРАЗМЕЧЕННЫМ."""
    from app.services.episodes import prepare_episode_replay

    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9007, message_id=90008, sent_at=now - timedelta(hours=2)
    )
    session.add(_failure(90008, now - timedelta(minutes=30)))
    session.add(_technical(90008))
    await session.flush()

    snapshot = await prepare_episode_replay(session, chat_ids={9007})
    assert 90008 not in snapshot.verdicts


@requires_db
async def test_a_real_verdict_still_takes_the_message_off_the_queue(session):
    """Обычный путь не изменился: успех закрывает сообщение навсегда."""
    from app.db.models import Classification

    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9006, message_id=90007, sent_at=now - timedelta(hours=2)
    )
    session.add(
        Classification(
            message_id=90007,
            model=PRIMARY,
            prompt_version=1502,
            source=SOURCE_MODEL,
            label="substantive",
            requires_response=False,
            is_substantive=True,
        )
    )
    await session.flush()

    assert await _pending_ids(session, now + timedelta(days=30)) == []


# ── 6. Технический вердикт: проверки по результату ────────────────────


@requires_db
async def test_a_technical_verdict_changes_nothing_for_the_engine(session, monkeypatch):
    """Движок обязан вести себя так, будто такой строки нет ВОВСЕ.

    Два одинаковых чата с одной и той же перепиской; в первом у сообщения
    клиента лежит технический вердикт, во втором — ничего. Эпизоды
    обязаны совпасть поле в поле.
    """
    from sqlalchemy import select

    from app.config import get_settings
    from app.db.models import (
        BusinessSide,
        Chat,
        ChatState,
        ChatTrackingPeriod,
        Interaction,
        Message,
        TransportActorKind,
    )
    from app.services.episodes import rebuild_interactions

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    for name in ("episode_rules_v2_since", "episode_rules_v3_since", "episode_rules_v4_since"):
        monkeypatch.setattr(
            get_settings(), name, datetime(2026, 1, 1, tzinfo=timezone.utc), raising=False
        )

    asked = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)

    async def _pair(chat_id: int, base: int) -> None:
        session.add(
            Chat(id=chat_id, tg_chat_id=chat_id, title="т", state=ChatState.TRACKED)
        )
        session.add(
            ChatTrackingPeriod(chat_id=chat_id, started_at=asked - timedelta(days=30))
        )
        session.add(
            Message(
                id=base,
                chat_id=chat_id,
                tg_message_id=base,
                sent_at=asked,
                text="Подскажите, пожалуйста, когда будет счёт?",
                business_side=BusinessSide.CLIENT,
                transport_actor_kind=TransportActorKind.HUMAN_USER,
            )
        )
        session.add(
            Message(
                id=base + 1,
                chat_id=chat_id,
                tg_message_id=base + 1,
                sent_at=asked + timedelta(minutes=5),
                text="Счёт готовим, пришлём сегодня",
                business_side=BusinessSide.COMPANY,
                transport_actor_kind=TransportActorKind.HUMAN_USER,
            )
        )

    await _pair(9200, 92000)  # здесь будет технический вердикт
    await _pair(9201, 92100)  # контроль: вердикта нет вовсе
    # Сообщения — отдельной записью: у `classification` внешний ключ на
    # `message`, а ORM-связи между ними нет, и порядок вставки сам по себе
    # не гарантирован.
    await session.flush()
    session.add(_failure(92000, asked + timedelta(minutes=20)))
    session.add(_technical(92000))
    await session.flush()

    await rebuild_interactions(session, now=asked + timedelta(hours=2))
    await session.flush()

    def _shape(episode: Interaction) -> tuple:
        return (
            episode.opened_at,
            episode.state,
            episode.first_reaction_at,
            episode.substantive_at,
            episode.handoff_at,
            episode.last_client_at,
            episode.client_messages,
            episode.ttfr_seconds,
        )

    with_technical = [
        _shape(row)
        for row in await session.scalars(
            select(Interaction).where(Interaction.chat_id == 9200).order_by(Interaction.opened_at)
        )
    ]
    without_any = [
        _shape(row)
        for row in await session.scalars(
            select(Interaction).where(Interaction.chat_id == 9201).order_by(Interaction.opened_at)
        )
    ]

    assert with_technical == without_any, "технический вердикт изменил разбор"
    assert with_technical, "контрольная переписка обязана давать обращение"


@requires_db
async def test_a_technical_verdict_is_not_the_last_verdict_on_the_screen(session):
    """Экран состояния не имеет права показать пропуск как свежую разметку."""
    from app.services.ai_stats import last_verdict_at

    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9202, message_id=92200, sent_at=now - timedelta(hours=2)
    )
    before = await last_verdict_at(session)
    session.add(_technical(92200))
    await session.flush()

    assert await last_verdict_at(session) == before


@requires_db
async def test_the_attempts_counter_survives_a_model_change(session):
    """Смена модели не обнуляет счёт попыток и не удлиняет срок повтора.

    Отказ записан под прежней моделью; после переезда она остаётся
    в `ai_accepted_models` (AI_PREVIOUS_MODELS), и очередь обязана видеть
    тот же счёт: иначе каждая смена модели давала бы сообщению заново
    три попытки, а чат стоял бы за ним ещё двадцать минут.
    """
    from sqlalchemy import select

    from app.db.models import Message
    from app.services.ai_stats import attempts_column

    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session, chat_id=9203, message_id=92300, sent_at=now - timedelta(hours=3)
    )
    session.add(_failure(92300, now - timedelta(minutes=10), attempts=2))
    await session.flush()

    new_model = "openai/gpt-oss-120b-v2"
    after_move = [new_model, PRIMARY, FALLBACK]

    counted = await session.scalar(
        select(attempts_column(after_move)).where(Message.id == 92300)
    )
    assert counted == 2, "счёт попыток потерян при смене модели"

    # И срок повтора остался вторым (15 минут), а не первым (5).
    async def _pending(moment, models):
        rows = await session.scalars(
            select(Message.id).where(*pending_conditions(models, now=moment))
        )
        return list(rows)

    assert await _pending(now, after_move) == []
    assert await _pending(now + timedelta(minutes=6), after_move) == [92300]

    # Прежняя модель выброшена из истории — её отказ перестаёт быть своим,
    # и сообщение честно начинает счёт заново. Строку подчистит воркер
    # в той же транзакции, что и следующую запись по сообщению.
    forgotten = await session.scalar(
        select(attempts_column([new_model])).where(Message.id == 92300)
    )
    assert forgotten == 0
    assert await _pending(now, [new_model]) == [92300]


async def test_the_last_attempt_without_a_fallback_asks_the_primary_and_gives_up(
    monkeypatch,
):
    """Резерв выключен — последняя попытка идёт на основную, а не падает.

    `AI_FALLBACK_MODEL` может быть пуст, и
    «третью попытку отдаём резерву» не имеет права превратиться в отказ
    воркера. Проверяется весь путь прохода, а не только клиент: ветку
    `force_fallback` ставит воркер, и именно он должен пережить её без
    настроенного резерва.
    """
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.db.models import BusinessSide
    from app.services.ai_stats import MAX_CLASSIFY_ATTEMPTS
    from app.worker import main as worker

    settings = get_settings()
    monkeypatch.setattr(settings, "ai_model", PRIMARY, raising=False)
    monkeypatch.setattr(settings, "ai_prompt_profile", "", raising=False)
    monkeypatch.setattr(settings, "ai_fallback_model", "", raising=False)
    monkeypatch.setattr(settings, "ai_monthly_token_limit", None, raising=False)
    monkeypatch.setattr(settings, "ai_shadow_mode", False, raising=False)

    row = SimpleNamespace(
        id=555,
        chat_id=77,
        text="Подскажите по счёту",
        has_media=False,
        media_kind=None,
        business_side=BusinessSide.CLIENT,
        sent_at=datetime.now(timezone.utc),
        needs_reclassification=False,
        thread_id=None,
        # Две попытки уже позади: эта — последняя.
        attempts=MAX_CLASSIFY_ATTEMPTS - 1,
    )
    stored: list = []

    class _Session:
        async def execute(self, query):
            columns = getattr(query, "column_descriptions", ())
            return SimpleNamespace(all=lambda: [row] if len(columns) >= 9 else [])

        def add(self, item):
            stored.append(item)

    @asynccontextmanager
    async def scope():
        yield _Session()

    class _Batch:
        async def context(self, _row):
            return (), ()

        def record_verdict(self, *args, **kwargs):
            return None

    http = _Http({PRIMARY: _timeout})

    class _Client(AiClient):
        async def month_tokens(self, session):
            return 0

        async def record_usage(self, *args, **kwargs):
            return None

    monkeypatch.setattr(worker, "session_scope", scope)
    async def prepare(session, rows):
        return _Batch()

    monkeypatch.setattr(worker, "prepare_contexts", prepare)

    client = _Client(circuit=FallbackCircuit())
    client._http = http
    result = await worker.classify_pending(client)

    assert [body["model"] for body in http.sent] == [PRIMARY], "звали не основную"
    assert result["failed"] == 1 and result["gave_up"] == 1
    assert [item.source for item in stored] == [SOURCE_MODEL, SOURCE_TECHNICAL]
    assert stored[0].attempts == MAX_CLASSIFY_ATTEMPTS
    assert stored[0].error and stored[1].error is None
    # Задержка учитывается и у отказавшего вызова, а не только у удачных.
    assert result["latency_ms_median"] is not None
    assert result["latency_ms_max"] is not None


# ── 7. Правка: та же механика повторов, что у всех ───────────────────


async def _classification_rows(session, message_id: int) -> list[tuple[int, str | None]]:
    """(attempts, error) всех строк сообщения — колонками, мимо identity map."""
    from sqlalchemy import select

    from app.db.models import Classification

    rows = await session.execute(
        select(Classification.attempts, Classification.error)
        .where(Classification.message_id == message_id)
        .order_by(Classification.id)
    )
    return [(int(attempts), error) for attempts, error in rows]


@requires_db
async def test_an_edit_resets_the_verdict_once_and_not_every_tick(session, monkeypatch):
    """Сброс производного у правки — ОДИН раз, счёт попыток его переживает.

    Границу проводит момент правки: строки старше него уходят, моложе —
    это текущий цикл разметки.
    """
    from contextlib import asynccontextmanager

    from app.db.models import Classification, Message
    from app.worker import main as worker

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(worker, "session_scope", scope)

    now = datetime.now(timezone.utc)
    edited_at = now - timedelta(minutes=20)
    await _chat_with_message(
        session,
        chat_id=9400,
        message_id=94000,
        sent_at=now - timedelta(hours=2),
        client=True,
    )
    message = await session.get(Message, 94000)
    message.needs_reclassification = True
    message.edited_at = edited_at
    # Вердикт ПРЕЖНЕГО текста: «спасибо» до правки, «а можно ещё акт?» после.
    session.add(
        Classification(
            message_id=94000,
            model=PRIMARY,
            prompt_version=1502,
            source=SOURCE_RULE,
            label="ack",
            requires_response=False,
            created_at=edited_at - timedelta(hours=1),
        )
    )
    await session.flush()

    # Тик 1: прежний вердикт снят, сообщение вернулось в очередь.
    assert await worker.reprocess_edited() == 1
    assert await _classification_rows(session, 94000) == [], "прежний вердикт обязан уйти"
    assert await _pending_ids(session, now) == [94000]

    # Провайдер отказал — строка отказа записана ПОСЛЕ правки.
    session.add(_failure(94000, now - timedelta(minutes=4)))
    await session.flush()

    # Тик 2: флаг всё ещё стоит, но сбрасывать больше нечего.
    assert await worker.reprocess_edited() == 1
    assert await _classification_rows(session, 94000) == [(1, "TimeoutError: ")], (
        "счёт попыток правки стёрт следующим же тиком"
    )
    assert await _pending_ids(session, now) == [], "пауза в 5 минут не соблюдена"
    assert await _pending_ids(session, now + timedelta(minutes=2)) == [94000]

    # Второй отказ — ждём 15 минут, а не минуту.
    await session.execute(
        Classification.__table__.update()
        .where(Classification.message_id == 94000)
        .values(attempts=2, created_at=now - timedelta(minutes=10))
    )
    assert await worker.reprocess_edited() == 1
    assert await _classification_rows(session, 94000) == [(2, "TimeoutError: ")]
    assert await _pending_ids(session, now) == []
    assert await _pending_ids(session, now + timedelta(minutes=6)) == [94000]


@requires_db
async def test_a_second_edit_during_the_pause_starts_the_count_over(
    session, monkeypatch
):
    """Правка во время паузы повтора — это другой текст, и счёт с начала.

    Граница «что производное, а что текущий цикл» — момент правки, поэтому
    вторая правка сдвигает её вперёд и снимает строку отказа, накопленную
    по прежнему тексту: ждать пятнадцать минут ради вопроса, которого
    в сообщении уже нет, незачем.
    """
    from contextlib import asynccontextmanager

    from app.db.models import Message
    from app.worker import main as worker

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(worker, "session_scope", scope)

    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session,
        chat_id=9401,
        message_id=94001,
        sent_at=now - timedelta(hours=2),
        client=True,
    )
    message = await session.get(Message, 94001)
    message.needs_reclassification = True
    message.edited_at = now - timedelta(minutes=20)
    session.add(_failure(94001, now - timedelta(minutes=10), attempts=2))
    await session.flush()

    assert await _pending_ids(session, now) == [], "сообщение в паузе повтора"

    message.edited_at = now - timedelta(seconds=30)
    await session.flush()

    assert await worker.reprocess_edited() == 1
    assert await _classification_rows(session, 94001) == [], (
        "счёт по прежнему тексту пережил правку"
    )
    assert await _pending_ids(session, now) == [94001]


@requires_db
async def test_a_forced_recheck_revives_a_message_that_gave_up(session, monkeypatch):
    """Осознанный переспрос поднимает и сообщение с техническим вердиктом.

    У `scripts/reclassify.py` и пересчёта сторон момента правки нет
    (`edited_at` пуст), поэтому границей служит сама РАЗРЕШАЮЩАЯ строка:
    пока она есть, сброс не делался, и снимается всё производное разом —
    включая технический вердикт и накопленный счёт попыток. Иначе
    исчерпавшее попытки сообщение осталось бы с флагом навсегда, занимая
    место в окне пятидесяти строк и не попадая в очередь.
    """
    from contextlib import asynccontextmanager

    from app.db.models import Message
    from app.worker import main as worker

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(worker, "session_scope", scope)

    now = datetime.now(timezone.utc)
    await _chat_with_message(
        session,
        chat_id=9402,
        message_id=94002,
        sent_at=now - timedelta(days=1),
        client=True,
    )
    message = await session.get(Message, 94002)
    message.needs_reclassification = True
    session.add(_failure(94002, now - timedelta(hours=5), attempts=MAX_CLASSIFY_ATTEMPTS))
    session.add(_technical(94002))
    await session.flush()

    assert await _pending_ids(session, now) == [], "попытки были исчерпаны"
    assert await worker.reprocess_edited() == 1
    assert await _classification_rows(session, 94002) == []
    assert await _pending_ids(session, now) == [94002], "переспрос не состоялся"


async def test_the_last_attempt_on_an_edited_message_ends_the_edit(monkeypatch, enabled):
    """Исчерпание попыток у правки — терминальный исход, а не вечный круг.

    Три вещи сразу: последняя попытка уходит на РЕЗЕРВ (счёт попыток дожил
    до неё), пишется технический вердикт, и флаг правки гасится. Без
    последнего сообщение возвращалось бы в выборку `reprocess_edited`
    каждым тиком, занимая место в окне пятидесяти строк, а его технический
    вердикт снимался бы тем же тиком.
    """
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.db.models import BusinessSide
    from app.worker import main as worker

    monkeypatch.setattr(enabled, "ai_shadow_mode", False, raising=False)

    row = SimpleNamespace(
        id=556,
        chat_id=78,
        text="А можно ещё акт сверки?",
        has_media=False,
        media_kind=None,
        business_side=BusinessSide.CLIENT,
        sent_at=datetime.now(timezone.utc),
        # Правленое сообщение: флаг стоит, две попытки уже позади.
        needs_reclassification=True,
        thread_id=None,
        attempts=MAX_CLASSIFY_ATTEMPTS - 1,
    )
    stored: list = []
    message = SimpleNamespace(id=556, needs_reclassification=True)

    class _Session:
        async def execute(self, query):
            columns = getattr(query, "column_descriptions", ())
            return SimpleNamespace(all=lambda: [row] if len(columns) >= 9 else [])

        def add(self, item):
            stored.append(item)

        async def get(self, model, key):
            return message if key == row.id else None

    @asynccontextmanager
    async def scope():
        yield _Session()

    class _Batch:
        async def context(self, _row):
            return (), ()

        def record_verdict(self, *args, **kwargs):
            return None

    async def prepare(session, rows):
        return _Batch()

    http = _Http({PRIMARY: _timeout, FALLBACK: _timeout})

    class _Client(AiClient):
        async def month_tokens(self, session):
            return 0

        async def record_usage(self, *args, **kwargs):
            return None

    monkeypatch.setattr(worker, "session_scope", scope)
    monkeypatch.setattr(worker, "prepare_contexts", prepare)

    client = _Client(circuit=FallbackCircuit())
    client._http = http
    result = await worker.classify_pending(client)

    assert [body["model"] for body in http.sent] == [FALLBACK], (
        "последняя попытка правки обязана уйти на резерв — ради этого счёт и ведут"
    )
    assert result["gave_up"] == 1
    assert [item.source for item in stored] == [SOURCE_MODEL, SOURCE_TECHNICAL]
    assert stored[0].attempts == MAX_CLASSIFY_ATTEMPTS
    assert message.needs_reclassification is False, (
        "флаг правки не погашен — сообщение вернётся в reprocess_edited навсегда"
    )
