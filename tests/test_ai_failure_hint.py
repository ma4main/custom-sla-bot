"""Понятная причина сбоя ИИ вместо сырого ответа провайдера.

Каждый тест — один класс сбоя, который бот называет по-человечески (истёкший
ключ, баланс, собственный потолок токенов); незнакомая причина остаётся как есть.
"""

from app.services.ai_health import BUDGET_REASON, down_message, failure_hint

# Ответ провайдера на истёкший ключ. Порядок полей в JSON у
# провайдера не закреплён, поэтому проверяем оба: узнавание не должно
# зависеть от того, что идёт первым — код ошибки или её текст.
EXPIRED_KEY_ERROR = (
    'HTTP 403: {"code":"AccessDenied","message":"API key verification '
    'failed: key is expired"}'
)
EXPIRED_KEY_ERROR_SWAPPED = (
    'HTTP 403: {"message":"API key verification failed: key is expired",'
    '"code":"AccessDenied"}'
)


def test_expired_key_is_named_a_key_problem():
    """Истёкший ключ узнаётся и в том порядке полей, и в обратном."""
    for reason in (EXPIRED_KEY_ERROR, EXPIRED_KEY_ERROR_SWAPPED):
        hint = failure_hint(reason)
        assert hint is not None, reason
        why, todo = hint
        assert "ключ" in why.lower()
        assert "set_ai_key.sh" in todo


def test_bare_unauthorized_is_a_key_problem_too():
    """401/403 без пояснений — тоже ключ: счёт отвечает 402 или словами."""
    assert failure_hint("HTTP 401: Unauthorized")[0] == failure_hint(EXPIRED_KEY_ERROR)[0]
    assert "ключ" in failure_hint("RuntimeError: HTTP 403: Forbidden")[0].lower()


def test_empty_balance_stays_a_money_problem():
    """Регресс: отказ по деньгам не должен превратиться в «замените ключ»."""
    for reason in (
        "HTTP 402: Payment Required",
        "RuntimeError: HTTP 403: insufficient balance",
        "HTTP 429: monthly quota exceeded",
    ):
        why, todo = failure_hint(reason)
        assert "баланс" in why.lower(), reason
        assert "пополн" in todo.lower()


def test_missing_model_is_named_a_catalogue_problem():
    for reason in (
        "HTTP 404: model not found",
        'RuntimeError: {"error":"The model `gpt-oss-120b` does not exist"}',
        "No such model in catalogue",
    ):
        why, todo = failure_hint(reason)
        assert "модель" in why.lower(), reason
        assert "AI_MODEL" in todo


def test_network_trouble_tells_to_wait():
    for reason in (
        "TimeoutError: ",
        "asyncio.TimeoutError: request timed out",
        "ClientConnectorError: Name or service not known",
        "HTTP 503: Service Unavailable",
        "HTTP 504: Gateway Timeout",
    ):
        why, todo = failure_hint(reason)
        assert "не отвеча" in why.lower(), reason
        assert "само" in todo.lower()


def test_our_own_token_ceiling_is_not_the_providers_fault():
    """Потолок AI_MONTHLY_TOKEN_LIMIT — наша настройка, не счёт Cloud.ru."""
    why, todo = failure_hint(BUDGET_REASON)
    assert "AI_MONTHLY_TOKEN_LIMIT" in why
    assert "баланс" not in why.lower() and "баланс" not in todo.lower()
    assert "баланс" not in down_message(BUDGET_REASON)


def test_unknown_reason_keeps_the_raw_answer():
    """Незнакомый сбой не выдумываем: сообщение остаётся прежним."""
    assert failure_hint("RuntimeError: something nobody has seen") is None
    assert failure_hint(None) is None
    assert failure_hint("") is None

    text = down_message("RuntimeError: something nobody has seen")
    assert "Причина: RuntimeError: something nobody has seen" in text


def test_down_message_for_expired_key_is_readable_and_safe():
    """Сообщение об истёкшем ключе вместо «HTTP 403: AccessDenied»."""
    text = down_message(EXPIRED_KEY_ERROR)

    assert "Истёк" in text
    assert "set_ai_key" in text
    assert "Что делать:" in text
    # Сырой ответ остаётся — без него не разобраться, если класс угадан
    # неверно. Но он экранирован: «<» из HTML-страницы шлюза ломает разбор
    # и срывает доставку сообщения о сбое.
    assert "Ответ провайдера:" in text
    assert "AccessDenied" in text
    assert "Алерты приостановлены" in text

    broken = down_message('HTTP 403: <b>key is expired</b> & gone')
    assert "<b>key is expired</b>" not in broken
    assert "&lt;b&gt;key is expired&lt;/b&gt; &amp; gone" in broken


def test_down_message_trims_a_wall_of_html():
    """Ответом бывает целая страница шлюза — в сообщение она целиком не идёт."""
    text = down_message("HTTP 502: " + "<div>Bad Gateway</div>" * 100)

    assert "не отвеча" in text.lower()
    assert "…" in text
    assert len(text) < 1500
