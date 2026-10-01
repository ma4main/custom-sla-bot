"""Резерв модели в классификаторе: без резерва запрос не меняется; переход после
`AI_FALLBACK_RETRIES` отказов в окне; на резерве — тексты, call_params и версия
промпта резервного профиля; возврат по двум удачным пробам; отказ резерва — ошибка."""

from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.services import ai
from app.services.ai import (
    PROMPT_PROFILES,
    AiClient,
    FallbackCircuit,
)
from tests.conftest import requires_db

PRIMARY = "openai/gpt-oss-120b"
FALLBACK = "deepseek-ai/DeepSeek-V4-Pro"

CLIENT_VERDICT = {"label": "request", "requires_response": True}


# ── Обвязка: подставной провайдер ──────────────────────────────────────


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
    """Провайдер, отвечающий по модели из тела запроса.

    Запоминает ВСЕ отправленные тела: проверять надо не только вердикт,
    но и что именно ушло в сеть — промпт, модель и call_params.
    """

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.sent: list[dict] = []

    def post(self, url, headers=None, json=None):  # noqa: A002 — сигнатура aiohttp
        self.sent.append(json)
        answer = self.answers[json["model"]]
        if callable(answer):
            answer = answer(len(self.sent))
        return answer


def _ok(verdict: dict, *, prompt: int = 100, completion: int = 20) -> _Response:
    return _Response(
        200,
        json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(verdict, ensure_ascii=False)},
                    }
                ],
                "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
            }
        ),
    )


def _fail(status: int = 503, body: str = "upstream is down") -> _Response:
    return _Response(status, body)


def _invalid() -> _Response:
    """HTTP 200, но в content не JSON."""
    return _Response(
        200,
        json.dumps(
            {
                "choices": [{"finish_reason": "stop", "message": {"content": "не знаю"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 5},
            }
        ),
    )


@pytest.fixture
def enabled(monkeypatch):
    """Боевая конфигурация: gpt-oss основная, DeepSeek резервная."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_model", PRIMARY, raising=False)
    monkeypatch.setattr(settings, "ai_prompt_profile", "", raising=False)
    monkeypatch.setattr(settings, "ai_fallback_model", FALLBACK, raising=False)
    monkeypatch.setattr(settings, "ai_fallback_prompt_profile", "", raising=False)
    monkeypatch.setattr(settings, "ai_fallback_retries", 3, raising=False)
    monkeypatch.setattr(settings, "ai_fallback_retry_window_seconds", 300, raising=False)
    monkeypatch.setattr(settings, "ai_fallback_probe_seconds", 1800, raising=False)
    monkeypatch.setattr(settings, "ai_fallback_restore_successes", 2, raising=False)
    monkeypatch.setattr(settings, "ai_monthly_token_limit", None, raising=False)
    return settings


@pytest.fixture
def disabled(monkeypatch):
    """Конфигурация без резерва."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_model", PRIMARY, raising=False)
    monkeypatch.setattr(settings, "ai_prompt_profile", "", raising=False)
    monkeypatch.setattr(settings, "ai_fallback_model", "", raising=False)
    monkeypatch.setattr(settings, "ai_monthly_token_limit", None, raising=False)
    return settings


def _client(http: _Http, *, circuit: FallbackCircuit | None = None) -> AiClient:
    client = AiClient(circuit=circuit or FallbackCircuit())
    client._http = http
    return client


async def _classify(client: AiClient, http: _Http) -> dict:
    return await client.classify_detailed("СИСТЕМА ОСНОВНОЙ", "вход", is_client=True)


# ── 1. Резерв выключен: ничего не изменилось ───────────────────────────


async def test_disabled_fallback_sends_exactly_the_old_request(disabled):
    """Без резерва уходит обычный запрос основного профиля."""
    http = _Http({PRIMARY: _ok(CLIENT_VERDICT)})
    client = _client(http)
    assert client.fallback_model == ""
    assert client.fallback_profile is None

    detail = await _classify(client, http)

    assert detail["verdict"] == {"label": "request", "requires_response": True}
    assert detail["fallback"] is False
    assert detail["model"] == PRIMARY
    assert http.sent == [
        {
            "model": PRIMARY,
            "messages": [
                {"role": "system", "content": "СИСТЕМА ОСНОВНОЙ"},
                {"role": "user", "content": "вход"},
            ],
            # call_params боевого профиля gptoss-p14, не резервного.
            "temperature": 0,
            "max_tokens": 800,
            "response_format": {"type": "json_object"},
            "reasoning_effort": "low",
        }
    ]


async def test_disabled_fallback_keeps_the_old_error_path(disabled):
    """Отказ провайдера — одна попытка и ошибка наружу, без второй модели."""
    http = _Http({PRIMARY: _fail()})
    client = _client(http)

    detail = await _classify(client, http)

    assert len(http.sent) == 1, "второй модели не существует — звать некого"
    assert detail["http_status"] == 503
    assert detail["error"].startswith("HTTP 503")
    assert detail["fallback"] is False


# ── 2. Переход на резерв после трёх отказов ────────────────────────────


async def test_single_failure_does_not_switch_to_the_fallback(enabled):
    """Одна помеха провайдера — ошибка и повтор следующим тиком, без резерва."""
    circuit = FallbackCircuit(retries=3, retry_window=300)
    http = _Http({PRIMARY: _fail(429, "rate limited"), FALLBACK: _ok(CLIENT_VERDICT)})
    client = _client(http, circuit=circuit)

    detail = await _classify(client, http)

    assert [body["model"] for body in http.sent] == [PRIMARY]
    assert detail["error"].startswith("HTTP 429")
    assert detail["fallback"] is False
    assert len(circuit.failures) == 1
    assert circuit.active is False


async def test_third_failure_switches_the_whole_classification_to_the_fallback(enabled):
    """Три отказа подряд — переход; третий вызов уже отвечает резерв."""
    circuit = FallbackCircuit(retries=3, retry_window=300)
    http = _Http({PRIMARY: _fail(), FALLBACK: _ok(CLIENT_VERDICT)})
    client = _client(http, circuit=circuit)

    first = await _classify(client, http)
    second = await _classify(client, http)
    assert (first["fallback"], second["fallback"]) == (False, False)
    assert circuit.active is False

    third = await _classify(client, http)

    assert circuit.active is True, "порог набран — работаем на резерве"
    assert third["fallback"] is True
    assert third["error"] is None
    assert third["verdict"]["label"] == "request"
    # Три попытки на основной и один вызов резерва — не больше.
    assert [body["model"] for body in http.sent] == [
        PRIMARY,
        PRIMARY,
        PRIMARY,
        FALLBACK,
    ]

    # Дальше основную не трогаем вовсе: провайдер лежит, платить за отказы
    # по два раза в минуту незачем.
    http.sent.clear()
    await _classify(client, http)
    assert [body["model"] for body in http.sent] == [FALLBACK]


async def test_invalid_json_from_the_primary_counts_as_a_failure(enabled):
    """«Невалидный или пустой ответ» — такой же отказ, как HTTP 5xx."""
    circuit = FallbackCircuit(retries=3, retry_window=300)
    http = _Http({PRIMARY: _invalid(), FALLBACK: _ok(CLIENT_VERDICT)})
    client = _client(http, circuit=circuit)

    for _ in range(2):
        detail = await _classify(client, http)
        assert detail["error"] is not None
        assert detail["fallback"] is False

    third = await _classify(client, http)
    assert third["fallback"] is True
    assert third["verdict"]["label"] == "request"


async def test_failures_spread_wider_than_the_window_do_not_switch(enabled, monkeypatch):
    """Три отказа за сутки — три помехи, а не сбой: резерв не включается."""
    circuit = FallbackCircuit(retries=3, retry_window=300)
    http = _Http({PRIMARY: _fail(), FALLBACK: _ok(CLIENT_VERDICT)})
    client = _client(http, circuit=circuit)

    clock = [1000.0]
    monkeypatch.setattr(ai, "monotonic", lambda: clock[0])
    for _ in range(3):
        detail = await _classify(client, http)
        assert detail["fallback"] is False
        clock[0] += 3600

    assert circuit.active is False
    assert len(circuit.failures) == 1, "прежние отказы вышли из окна"


async def test_a_success_on_the_primary_clears_the_failure_streak(enabled):
    """Удачный вердикт обнуляет счётчик: «подряд» значит подряд."""
    circuit = FallbackCircuit(retries=3, retry_window=300)
    answers = {PRIMARY: _fail(), FALLBACK: _ok(CLIENT_VERDICT)}
    http = _Http(answers)
    client = _client(http, circuit=circuit)

    await _classify(client, http)
    await _classify(client, http)
    answers[PRIMARY] = _ok(CLIENT_VERDICT)
    await _classify(client, http)
    assert circuit.failures == []

    answers[PRIMARY] = _fail()
    for _ in range(2):
        assert (await _classify(client, http))["fallback"] is False
    assert circuit.active is False, "два отказа после успеха — ещё не порог"


# ── 3. На резерве уходит резервный промпт и его версия ─────────────────


async def test_the_fallback_call_uses_the_ds1b_profile_verbatim(enabled):
    """Тексты ds1b, версия 1402 в базу, call_params ds1b, модель — резервная."""
    circuit = FallbackCircuit(retries=1, retry_window=300)
    http = _Http({PRIMARY: _fail(), FALLBACK: _ok(CLIENT_VERDICT)})
    client = _client(http, circuit=circuit)
    ds1b = PROMPT_PROFILES["deepseek-ds1b"]

    detail = await _classify(client, http)

    assert detail["fallback"] is True
    assert detail["model"] == FALLBACK
    assert detail["profile"] == "deepseek-ds1b"
    assert detail["prompt_db_version"] == 1402 == ds1b.db_version

    fallback_body = http.sent[-1]
    assert fallback_body["model"] == FALLBACK
    # Системный промпт — текст ds1b, а НЕ то, что передал вызывающий.
    assert fallback_body["messages"][0]["content"] is ds1b.client_system
    assert fallback_body["messages"][0]["content"] != "СИСТЕМА ОСНОВНОЙ"
    # Вход тот же: формат входа у профилей общий, его строит вызывающий.
    assert fallback_body["messages"][1]["content"] == "вход"
    # call_params ds1b: у него reasoning объектом и max_tokens 500,
    # у основного профиля — верхнеуровневый reasoning_effort и 800.
    assert fallback_body["max_tokens"] == 500
    assert fallback_body["reasoning"] == {"effort": "none"}
    assert "reasoning_effort" not in fallback_body


async def test_the_company_side_gets_the_company_text_of_the_fallback(enabled):
    """Сообщение компании размечается company-промптом резерва, не client."""
    circuit = FallbackCircuit(retries=1, retry_window=300)
    verdict = {"label": "substantive", "is_substantive": True}
    http = _Http({PRIMARY: _fail(), FALLBACK: _ok(verdict)})
    client = _client(http, circuit=circuit)
    ds1b = PROMPT_PROFILES["deepseek-ds1b"]

    detail = await client.classify_detailed("СИСТЕМА", "вход", is_client=False)

    assert detail["fallback"] is True
    assert http.sent[-1]["messages"][0]["content"] is ds1b.company_system


async def test_explicit_fallback_profile_setting_wins_over_the_model_rule(
    enabled, monkeypatch
):
    """AI_FALLBACK_PROMPT_PROFILE перебивает выбор профиля по модели."""
    monkeypatch.setattr(
        get_settings(), "ai_fallback_prompt_profile", "gptoss-p14", raising=False
    )
    client = _client(_Http({}), circuit=FallbackCircuit())
    # По модели DeepSeek-V4-Pro был бы deepseek-ds1b; настройка сильнее.
    assert client.fallback_profile.profile_id == "gptoss-p14"
    assert client.fallback_profile.db_version == 1502


async def test_fallback_model_is_accepted_as_our_own_markup(enabled):
    """Вердикты резерва должны читаться движком как действительные."""
    accepted = get_settings().ai_accepted_models
    assert accepted[0] == PRIMARY
    assert FALLBACK in accepted, (
        "без резервной модели в ai_accepted_models движок счёл бы её "
        "разметку чужой и отправил бы сообщения на переспрос за деньги"
    )


# ── 4. Возврат на основную — по двум удачным пробам ────────────────────


async def test_probe_is_not_called_while_the_primary_is_healthy(enabled):
    """Резерв не включён — пробовать нечего и платить не за что."""
    client = _client(_Http({}), circuit=FallbackCircuit())
    assert await client.maybe_probe_primary() is None


async def test_restore_needs_two_successful_probes_in_a_row(enabled, monkeypatch):
    circuit = FallbackCircuit(
        retries=1, retry_window=300, probe_seconds=1800, restore_successes=2
    )
    answers = {PRIMARY: _fail(), FALLBACK: _ok(CLIENT_VERDICT)}
    http = _Http(answers)
    client = _client(http, circuit=circuit)
    clock = [1000.0]
    monkeypatch.setattr(ai, "monotonic", lambda: clock[0])

    assert (await _classify(client, http))["fallback"] is True
    assert circuit.active is True

    # Полчаса не прошло — пробы нет.
    clock[0] += 1799
    assert await client.maybe_probe_primary() is None

    # Первая проба: основная всё ещё лежит.
    clock[0] += 1
    http.sent.clear()
    result = await client.maybe_probe_primary()
    assert result is not None and result["ok"] is False and result["restored"] is False
    assert [body["model"] for body in http.sent] == [PRIMARY]
    assert circuit.active is True

    # Основная поднялась. Одной удачной пробы мало.
    answers[PRIMARY] = _ok({"ok": True})
    clock[0] += 1800
    first = await client.maybe_probe_primary()
    assert first["ok"] is True and first["restored"] is False
    assert circuit.active is True, "одна проба — не доказательство"

    # Вторая подряд — возврат.
    clock[0] += 1800
    second = await client.maybe_probe_primary()
    assert second["ok"] is True and second["restored"] is True
    assert circuit.active is False
    assert circuit.failures == []

    # И классификация снова идёт на основную.
    answers[PRIMARY] = _ok(CLIENT_VERDICT)
    http.sent.clear()
    detail = await _classify(client, http)
    assert detail["fallback"] is False
    assert [body["model"] for body in http.sent] == [PRIMARY]


async def test_a_failed_probe_resets_the_restore_streak(enabled, monkeypatch):
    """Успех, потом отказ — счёт начинается заново, а не «одна уже была»."""
    circuit = FallbackCircuit(
        retries=1, retry_window=300, probe_seconds=1800, restore_successes=2
    )
    answers = {PRIMARY: _ok({"ok": True}), FALLBACK: _ok(CLIENT_VERDICT)}
    http = _Http(answers)
    client = _client(http, circuit=circuit)
    clock = [1000.0]
    monkeypatch.setattr(ai, "monotonic", lambda: clock[0])
    circuit.active = True
    circuit.next_probe_at = clock[0]

    assert (await client.maybe_probe_primary())["ok"] is True
    assert circuit.probe_successes == 1

    answers[PRIMARY] = _fail()
    clock[0] += 1800
    assert (await client.maybe_probe_primary())["ok"] is False
    assert circuit.probe_successes == 0
    assert circuit.active is True

    answers[PRIMARY] = _ok({"ok": True})
    clock[0] += 1800
    assert (await client.maybe_probe_primary())["restored"] is False
    clock[0] += 1800
    assert (await client.maybe_probe_primary())["restored"] is True


async def test_probe_spends_a_minimal_call_not_a_classification(enabled, monkeypatch):
    """Проба — десятки токенов служебного промпта, не боевой вход."""
    circuit = FallbackCircuit(retries=1, probe_seconds=1800, restore_successes=2)
    http = _Http({PRIMARY: _ok({"ok": True}, prompt=18, completion=6)})
    client = _client(http, circuit=circuit)
    clock = [1000.0]
    monkeypatch.setattr(ai, "monotonic", lambda: clock[0])
    circuit.active = True
    circuit.next_probe_at = clock[0]

    result = await client.maybe_probe_primary()

    assert result["usage"] == (18, 6)
    body = http.sent[-1]
    assert body["model"] == PRIMARY
    assert body["messages"][0]["content"] == ai.PROBE_SYSTEM
    assert body["messages"][1]["content"] == ai.PROBE_USER
    assert len(ai.PROBE_SYSTEM) + len(ai.PROBE_USER) < 200


# ── 5. Отказ резерва — ошибка наружу ───────────────────────────────────


async def test_when_the_fallback_fails_too_the_error_surfaces(enabled):
    """Обе модели лежат — ошибка наружу, вердикта нет, повтор следующим тиком."""
    circuit = FallbackCircuit(retries=1, retry_window=300)
    http = _Http({PRIMARY: _fail(), FALLBACK: _fail(500, "fallback is down")})
    client = _client(http, circuit=circuit)

    detail = await _classify(client, http)

    assert detail["verdict"] is None
    assert detail["error"].startswith("HTTP 500")
    assert detail["fallback"] is True, "какая модель отвечала — видно в записи"
    assert detail["model"] == FALLBACK
    assert circuit.active is True, "резерв упал, но основная не воскресла"


# ── 6. Проба провайдера в простое знает про резерв ─────────────────────


class _CatalogResponse:
    def __init__(self, status: int, payload) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_CatalogResponse":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def json(self, content_type=None):
        return self._payload


class _CatalogHttp:
    def __init__(self, ids: list[str], status: int = 200) -> None:
        self._response = _CatalogResponse(
            status, {"data": [{"id": model} for model in ids]}
        )

    def get(self, url, headers=None):
        return self._response


async def test_probe_reports_the_fallback_when_the_primary_is_gone(enabled):
    """Основная пропала, резерв в каталоге — провайдер жив, алерты не глушим."""
    client = _client(_Http({}), circuit=FallbackCircuit())
    client._http = _CatalogHttp([FALLBACK, "other/model"])

    result = await client.probe_detailed()

    assert result["error"] is None, "классификация продолжится — это не сбой"
    assert result["fallback"] == FALLBACK
    assert result["primary_error"] == f"модель недоступна в каталоге ({PRIMARY})"
    assert await client.probe() is None


async def test_probe_still_fails_when_both_models_are_gone(enabled):
    client = _client(_Http({}), circuit=FallbackCircuit())
    client._http = _CatalogHttp(["some/unrelated-model"])

    result = await client.probe_detailed()

    assert result["error"] == f"модель недоступна в каталоге ({PRIMARY})"
    assert result["fallback"] is None


async def test_probe_is_silent_about_the_fallback_while_the_primary_is_fine(enabled):
    client = _client(_Http({}), circuit=FallbackCircuit())
    client._http = _CatalogHttp([PRIMARY, FALLBACK])

    result = await client.probe_detailed()

    assert result["error"] is None
    assert result["fallback"] is None, "работаем на основной — резерв не при чём"


async def test_health_state_carries_the_fallback_model(enabled):
    """Состояние ИИ помнит резерв: экран состояния не должен врать «включён»."""
    from datetime import datetime, timezone

    from app.services.ai_health import (
        INITIAL,
        OUTCOME_SUCCESS,
        STATUS_OK,
        next_state,
    )

    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    state = next_state(
        dict(INITIAL), OUTCOME_SUCCESS, None, now, attempted=5, failed=0, fallback=FALLBACK
    )
    assert state["status"] == STATUS_OK, "вердикты идут — алерты не приостанавливаются"
    assert state["fallback"] == FALLBACK

    back = next_state(state, OUTCOME_SUCCESS, None, now, attempted=5, failed=0)
    assert back["fallback"] is None, "вернулись на основную — резерв в состоянии не висит"


# ── 7. Запись вердикта резерва в базу ──────────────────────────────────
# Требует PostgreSQL: в `Classification` ложатся резервная модель и её версия
# промпта, а не то, что стоит в настройках.


@requires_db
@pytest.mark.usefixtures("enabled")
async def test_worker_records_the_fallback_model_and_version(session, monkeypatch):
    from sqlalchemy import select

    from app.db.models import Classification
    from app.worker import main as worker
    from tests.test_classify_context_v11 import _chat, _message

    from contextlib import asynccontextmanager

    chat = await _chat(session, -100900901, "Синтетический чат резерва")
    message = await _message(session, chat, 1, text="Подготовьте документ", minutes=0)

    @asynccontextmanager
    async def local_scope():
        yield session
        await session.flush()

    circuit = FallbackCircuit(retries=1, retry_window=300)
    http = _Http({PRIMARY: _fail(), FALLBACK: _ok(CLIENT_VERDICT)})
    client = _client(http, circuit=circuit)

    monkeypatch.setattr(worker, "session_scope", local_scope)
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)

    stats = await worker.classify_pending(client)

    assert stats["fallback"] == FALLBACK
    record = (
        await session.execute(
            select(Classification).where(Classification.message_id == message.id)
        )
    ).scalar_one()
    assert record.model == FALLBACK, "иначе движок не узнает собственную разметку"
    assert record.prompt_version == 1402, "версия промпта — резервного профиля"
    assert record.error is None
    assert record.label == "request"
