"""Проба провайдера в простое.

Воркер раз в полчаса без контакта с провайдером запрашивает бесплатный
GET /models, а исход идёт в ту же машину состояний, что и вердикты:
три неудачи — «сбой», одно сообщение владельцу.
"""

from datetime import datetime, timedelta, timezone

import aiohttp

from app.services import ai
from app.services.ai_health import (
    FAILURE_THRESHOLD,
    INITIAL,
    OUTCOME_FAILURE,
    OUTCOME_IDLE,
    OUTCOME_SUCCESS,
    PROBE_AFTER_MINUTES,
    STATUS_DOWN,
    STATUS_OK,
    last_contact_at,
    next_state,
    probe_due,
)

NOW = datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc)


def test_billing_hint_recognises_exhausted_balance():
    """402/403 или слова про баланс — подсказка «проверьте счёт»."""
    from app.services.ai_health import billing_hint, down_message

    assert billing_hint("RuntimeError: HTTP 402: {\"error\": \"Payment Required\"}")
    assert billing_hint("HTTP 403: quota exceeded for project")
    assert billing_hint("RuntimeError: HTTP 429: insufficient balance")
    assert billing_hint("HTTP 500: Internal Server Error") is None
    assert billing_hint("TimeoutError: ") is None
    assert billing_hint(None) is None
    assert "баланс" in down_message("HTTP 402: Payment Required")
    assert "баланс" not in down_message("TimeoutError")


def test_success_records_last_contact_and_idle_keeps_it():
    state = next_state(dict(INITIAL), OUTCOME_SUCCESS, None, NOW, attempted=5, failed=0)
    assert last_contact_at(state) == NOW

    later = next_state(state, OUTCOME_IDLE, None, NOW + timedelta(minutes=10))
    assert last_contact_at(later) == NOW, "пустая очередь контакт не стирает"


def test_probe_is_due_only_after_silence():
    fresh = next_state(dict(INITIAL), OUTCOME_SUCCESS, None, NOW, attempted=1)
    assert not probe_due(fresh, NOW + timedelta(minutes=PROBE_AFTER_MINUTES - 1))
    assert probe_due(fresh, NOW + timedelta(minutes=PROBE_AFTER_MINUTES))
    assert probe_due(dict(INITIAL), NOW), "контакта не было никогда — проверять сразу"


def test_failed_probes_bring_the_provider_down_like_failed_passes():
    state = next_state(dict(INITIAL), OUTCOME_SUCCESS, None, NOW, attempted=1)
    for step in range(FAILURE_THRESHOLD):
        state = next_state(
            state, OUTCOME_FAILURE, "TimeoutError", NOW + timedelta(minutes=31 + step),
            attempted=1, failed=1,
        )
    assert state["status"] == STATUS_DOWN
    assert state["reason"] == "TimeoutError"
    # Контакта во время сбоя не было — отметка осталась старой.
    assert last_contact_at(state) == NOW

    recovered = next_state(state, OUTCOME_SUCCESS, None, NOW + timedelta(hours=1), attempted=1)
    assert recovered["status"] == STATUS_OK
    assert last_contact_at(recovered) == NOW + timedelta(hours=1)


# ── Проба смотрит не только на код ответа, но и на состав каталога ──────
# Модель может пропасть из каталога, пока /models отвечает 200: проба
# обязана ловить это раньше первого провала классификации.


class _FakeResponse:
    def __init__(self, status: int, payload=None, *, bad_json: bool = False) -> None:
        self.status = status
        self._payload = payload
        self._bad_json = bad_json

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False

    async def json(self, content_type=None):
        if self._bad_json:
            raise aiohttp.ContentTypeError(None, ())
        return self._payload


class _FakeHttp:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    def get(self, url, headers=None):
        return self._response


async def test_probe_ok_when_configured_model_is_in_catalog():
    client = ai.AiClient()
    client.model = "GigaChat/GigaChat-2-Max"
    client._http = _FakeHttp(
        _FakeResponse(200, {"data": [{"id": "GigaChat/GigaChat-2-Max"}, {"id": "other"}]})
    )
    assert await client.probe() is None


async def test_probe_flags_configured_model_missing_from_catalog():
    client = ai.AiClient()
    client.model = "GigaChat/GigaChat-2-Max"
    client._http = _FakeHttp(_FakeResponse(200, {"data": [{"id": "some-other-model"}]}))
    assert await client.probe() == "модель недоступна в каталоге (GigaChat/GigaChat-2-Max)"


async def test_probe_keeps_old_behaviour_when_catalog_has_no_json():
    """Каталог 200, но не JSON (content-type другой) — судить нечем, поведение прежнее."""
    client = ai.AiClient()
    client.model = "GigaChat/GigaChat-2-Max"
    client._http = _FakeHttp(_FakeResponse(200, bad_json=True))
    assert await client.probe() is None


async def test_probe_passes_when_catalog_has_no_data_list():
    client = ai.AiClient()
    client.model = "GigaChat/GigaChat-2-Max"
    client._http = _FakeHttp(_FakeResponse(200, {"unexpected": "shape"}))
    assert await client.probe() is None


async def test_probe_reports_http_errors():
    client = ai.AiClient()
    client.model = "GigaChat/GigaChat-2-Max"
    client._http = _FakeHttp(_FakeResponse(403))
    assert await client.probe() == "HTTP 403 на /models"
