"""Тексты интерфейса: каждый тест закрепляет одну формулировку для пользователя."""


def test_own_token_budget_is_not_a_billing_problem():
    """Исчерпание НАШЕГО лимита токенов не называется проблемой счёта Cloud.ru."""
    from app.services.ai_health import BUDGET_REASON, billing_hint, down_message

    assert billing_hint(BUDGET_REASON) is None
    assert "баланс" not in down_message(BUDGET_REASON)
    assert billing_hint("HTTP 429: monthly quota exceeded"), "отказ провайдера по-прежнему узнаётся"


def test_down_message_escapes_provider_html():
    """«<» из HTML-страницы шлюза экранируется, иначе сообщение о сбое не доставится."""
    from app.services.ai_health import down_message

    text = down_message("HTTP 502: <html><body>Bad Gateway</body></html>")
    assert "<html>" not in text and "&lt;html&gt;" in text


def test_long_durations_are_days():
    from app.services.transcript import fmt_duration

    assert fmt_duration(47 * 3600 + 5 * 60) == "47 ч 05 мин"
    assert fmt_duration(50 * 3600) == "2 дн 2 ч"
    assert fmt_duration(200 * 3600) == "8 дн 8 ч"


def test_specialist_deadline_follows_the_threshold():
    """Срок специалиста называется одинаково в отчёте и алерте, по порогу."""
    from app.services.transcript import specialist_deadline_label

    assert specialist_deadline_label(1440) == "то же время следующего рабочего дня"
    assert specialist_deadline_label(240) == "4 ч 00 мин после передачи"


def test_since_has_a_russian_label_in_the_audit_log():
    from app.services.settings_store import field_label

    assert field_label("work_calendar", "since") == "Действует с"



def test_verdict_labels_cover_every_no_response_label():
    """У каждой метки «ответ не требуется» есть русская подпись в срезе отчёта."""
    from app.bot.handlers.reports import _VERDICT_LABELS
    from app.services.ai import _NO_RESPONSE_LABELS

    assert set(_NO_RESPONSE_LABELS) <= set(_VERDICT_LABELS)
