"""Ответ модели проверяется схемой, а не принимается как есть.

`response_format: json_object` гарантирует только валидный JSON, но не нужные
поля нужных типов: вердикт с чужим label или строкой вместо булева отбрасывается.
"""

import pytest

from app.services.ai import validate_verdict


def test_client_verdict_passes_through():
    assert validate_verdict(
        {"label": "request", "requires_response": True}, is_client=True
    ) == {"label": "request", "requires_response": True}


def test_company_verdict_carries_the_label():
    """По сообщению компании модель возвращает метку, а флаг выводится из неё.

    Два источника правды (метка и отдельный булев флаг) разошлись бы
    на первом же ответе модели.
    """
    assert validate_verdict({"label": "handoff"}, is_client=False) == {
        "label": "handoff",
        "is_substantive": False,
        "answers_request_id": None,
    }
    assert validate_verdict({"label": "substantive"}, is_client=False) == {
        "label": "substantive",
        "is_substantive": True,
        "answers_request_id": None,
    }
    assert validate_verdict({"label": "ack"}, is_client=False) == {
        "label": "ack",
        "is_substantive": False,
        "answers_request_id": None,
    }


def test_unknown_company_label_is_rejected():
    with pytest.raises(RuntimeError):
        validate_verdict({"label": "maybe"}, is_client=False)


def test_old_company_format_still_accepted():
    """Вердикты, записанные до появления меток, лежат в базе и используются."""
    assert validate_verdict({"is_substantive": True}, is_client=False) == {
        "label": "substantive",
        "is_substantive": True,
        "answers_request_id": None,
    }


def test_unknown_label_is_rejected():
    """Чужая метка — признак того, что модель отвечает не на ту задачу."""
    with pytest.raises(RuntimeError, match="label"):
        validate_verdict({"label": "urgent", "requires_response": True}, is_client=True)


def test_label_case_and_spaces_are_tolerated():
    assert validate_verdict({"label": " Question ", "requires_response": 1}, is_client=True) == {
        "label": "question",
        "requires_response": True,
    }


def test_string_booleans_are_accepted():
    assert validate_verdict({"is_substantive": "true"}, is_client=False) == {
        "label": "substantive",
        "is_substantive": True,
        "answers_request_id": None,
    }


def test_missing_flag_is_derived_from_label():
    """Метка сильнее флага, поэтому терять вердикт целиком не за чем."""
    assert validate_verdict({"label": "ack"}, is_client=True) == {
        "label": "ack",
        "requires_response": False,
    }
    assert validate_verdict({"label": "question"}, is_client=True) == {
        "label": "question",
        "requires_response": True,
    }


def test_unreadable_company_verdict_is_rejected():
    """Ни метки, ни флага — выводить не из чего, только брак."""
    with pytest.raises(RuntimeError, match="сообщению компании"):
        validate_verdict({"reasoning": "похоже на отписку"}, is_client=False)


def test_non_object_is_rejected():
    with pytest.raises(RuntimeError):
        validate_verdict(["request"], is_client=True)


def test_extra_fields_do_not_leak_through():
    """Наружу уходят только известные поля, лишнее из ответа отбрасывается."""
    verdict = validate_verdict(
        {"label": "info", "requires_response": False, "confidence": 0.9, "note": "x"},
        is_client=True,
    )
    assert verdict == {"label": "info", "requires_response": False}


def test_offline_label_means_no_chat_answer():
    """«Позвоните мне» — ответ уйдёт вне чата, ждать его в переписке нельзя."""
    from app.services.ai import CLIENT_LABELS, validate_verdict

    assert "offline" in CLIENT_LABELS
    # Флаг модели противоречит метке — метка сильнее (движок трактует так же).
    verdict = validate_verdict(
        {"label": "offline", "requires_response": None}, is_client=True
    )
    assert verdict == {"label": "offline", "requires_response": False}


# ── Контракт v10 ──────────────────────────────────────────────────────────


def test_new_client_labels_are_accepted_and_need_no_answer():
    """addition/correction/answer — не новое дело, ответа компании не ждут."""
    for label in ("addition", "correction", "answer"):
        assert validate_verdict({"label": label}, is_client=True) == {
            "label": label,
            "requires_response": False,
        }


def test_requires_response_is_derived_from_the_label_not_asked():
    """Флаг модели противоречит метке — побеждает метка (контракт v10).

    Два источника правды на одном сообщении разошлись бы на первом же
    ответе: движок эпизодов читает метку, а в базу писался бы флаг.
    """
    assert validate_verdict(
        {"label": "answer", "requires_response": True}, is_client=True
    ) == {"label": "answer", "requires_response": False}
    assert validate_verdict(
        {"label": "request", "requires_response": False}, is_client=True
    ) == {"label": "request", "requires_response": True}


def test_only_request_and_question_require_an_answer():
    from app.services.ai import CLIENT_LABELS

    waiting = {
        label
        for label in CLIENT_LABELS
        if validate_verdict({"label": label}, is_client=True)["requires_response"]
    }
    assert waiting == {"request", "question", "mixed_request"}


def test_promise_is_not_substantive_and_not_a_handoff():
    """«Уточню и напишу» — обещание вернуться самому.

    Обращение оно не закрывает (не substantive) и второй слой SLA
    не открывает (не handoff): передал бы — писал бы другой человек.
    """
    from app.services.ai import COMPANY_LABELS

    assert "promise" in COMPANY_LABELS
    verdict = validate_verdict({"label": "promise"}, is_client=False)
    assert verdict["is_substantive"] is False
    assert verdict["label"] == "promise"


def test_answers_request_id_must_come_from_the_open_items():
    """Номер обращения принимается только из списка, поданного во вход."""
    assert validate_verdict(
        {"label": "substantive", "answers_request_id": 4821},
        is_client=False,
        open_request_ids=[4821, 4830],
    )["answers_request_id"] == 4821

    # Номер не из списка — не брак вердикта: метка обычно верна, а связь
    # просто неизвестна. Платить за повтор из-за промаха в связи дороже.
    assert validate_verdict(
        {"label": "substantive", "answers_request_id": 999},
        is_client=False,
        open_request_ids=[4821],
    )["answers_request_id"] is None

    # Список не подан вовсе (сообщение клиента, пустой чат) — связи нет.
    assert validate_verdict(
        {"label": "substantive", "answers_request_id": 4821}, is_client=False
    )["answers_request_id"] is None


def test_answers_request_id_tolerates_the_hash_form():
    """Модель повторяет номер так, как видела его во входе: «#4821»."""
    assert validate_verdict(
        {"label": "substantive", "answers_request_id": "#4821"},
        is_client=False,
        open_request_ids=[4821],
    )["answers_request_id"] == 4821


def test_boolean_is_not_a_request_id():
    """`true` из JSON — не обращение №1: bool в Python подкласс int."""
    assert validate_verdict(
        {"label": "substantive", "answers_request_id": True},
        is_client=False,
        open_request_ids=[1],
    )["answers_request_id"] is None


def test_legacy_info_keeps_the_model_flag():
    """`info` — устаревшая метка v9: флаг для неё остаётся за моделью.

    Для меток v10 флаг выводится из метки.
    """
    assert validate_verdict({"label": "info", "requires_response": True}, is_client=True) == {
        "label": "info",
        "requires_response": True,
    }
    assert validate_verdict({"label": "info"}, is_client=True) == {
        "label": "info",
        "requires_response": False,
    }
