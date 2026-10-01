"""Реплика компании с меткой `other` как первая реакция (правила v4).

Реплика `other` (не чистое приветствие) — первая реакция всем открытым
обращениям без реакции, как `ack`. Больше она ничего не делает: не отвечает
по существу, не передаёт, не закрывает и окно «ждём клиента» не трогает.
"""

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

import pytest

from app.db.models import InteractionState
from app.services.episodes import rebuild_interactions
from tests.test_classify_context_v11 import COMPANY, REQUEST, T0, batch, message
from tests.test_engine_v4_rules import (
    CLIENT_A, CLIENT_B, COMPANY_ACK, COMPANY_OTHER, COMPANY_SUBSTANTIVE,
    POLINA, IRINA, STAFF_KSENIA, STAFF_IRINA, members, msk, replay,
)

BANK_UPLOAD_REQUEST = "Добрый день, выгрузите в банк завтра"
BANK_UPLOAD_REPLY = "Добрый день, ПП в банке"
DOCUMENTS_REQUEST = (
    "Вера, сейчас письмо вам напишу, нужно в течении дня мне "
    "прислать документы."
)
QUESTION_TO_THE_CLIENT_BY_NAME = (
    "Жанна, может быть пока справка идет, сделать скрин из Личного "
    "кабинета налогоплательщика?"
)
# Объявления без адресата: метка `other` делает реакцией и их.
ANNOUNCE_CHAT_RESTORED = "Работа чата восстановлена"
ANNOUNCE_CHAT_DELETION = "(К) Людмила, данный чат в понедельник будет удален."

SIGN_RECONCILIATION_REQUEST = "Добрый день. Подпишите пожалуйста в эдо акт сверки"


def _reply_case(company_text, *, client_text=BANK_UPLOAD_REQUEST,
                verdict=COMPANY_OTHER, minutes=20, signature=IRINA):
    """Просьба клиента в 10:00 → одна реплика компании через `minutes`.

    Порог первой реакции — 30 рабочих минут, поэтому реплика в 10:20
    успевает, а её отсутствие к вечеру даёт алерт первого слоя.
    """
    messages = [
        message(3343, seconds=msk(10, 0), author=CLIENT_A, text=client_text),
        message(3484, COMPANY, seconds=msk(10, minutes),
                text=signature + company_text),
    ]
    return messages, {3343: REQUEST, 3484: verdict}


async def _run(messages, verdicts, **kwargs):
    kwargs.setdefault("staff", {3484: STAFF_IRINA})
    return await replay(messages, verdicts, **kwargs)


# ── Реплика `other` — первая реакция ─────────────────────────────────────


@pytest.mark.parametrize("client_text, company_text", [
    (BANK_UPLOAD_REQUEST, BANK_UPLOAD_REPLY),
    (BANK_UPLOAD_REQUEST, QUESTION_TO_THE_CLIENT_BY_NAME),
    (DOCUMENTS_REQUEST, QUESTION_TO_THE_CLIENT_BY_NAME),
])
async def test_an_other_reply_is_the_first_reaction(client_text, company_text):
    """Реплика `other` гасит срок первого слоя так же, как `ack`.

    Состояние REACTED обязательно: кандидатов первого слоя `alerts.py`
    отбирает по `state is OPEN`.
    """
    items, _ = await _run(*_reply_case(company_text, client_text=client_text))
    item = items[3343]
    assert item.first_reaction_message_id == 3484
    assert item.first_reaction_staff_id == STAFF_IRINA
    assert item.state is InteractionState.REACTED
    assert item.sla_breached is False
    assert item.ttfr_seconds == 20 * 60


async def test_a_late_other_reply_is_still_reported_as_breached():
    """Реакция не переписывает просрочку: поздний отклик остаётся поздним."""
    items, _ = await _run(*_reply_case(BANK_UPLOAD_REPLY, minutes=45))
    assert items[3343].first_reaction_message_id == 3484
    assert items[3343].sla_breached is True


async def test_an_other_reply_does_not_answer_hand_over_or_close():
    """Чего реплика `other` НЕ делает: второй слой, передача, закрытие, состав."""
    items, result = await _run(*_reply_case(QUESTION_TO_THE_CLIENT_BY_NAME))
    item = items[3343]
    assert item.handoff_at is None
    assert item.substantive_at is None
    assert item.substantive_message_id is None
    assert item.state is not InteractionState.ANSWERED
    assert members(result)[3343] == [3343]
    assert result["answers"] == []
    assert result["interactions"] == 1


async def test_a_pure_greeting_never_becomes_a_reaction():
    """Чистое приветствие (R-19) реакцией не становится и с меткой `other`."""
    items, _ = await _run(*_reply_case("Добрый день", verdict=COMPANY_OTHER))
    assert items[3343].first_reaction_at is None
    assert items[3343].state is InteractionState.OPEN


async def test_a_broadcast_never_becomes_a_reaction():
    """Рассылка (R-19) отсеивается раньше, чем читается метка `other`.

    Вопрос и второе лицо в тексте нарочно: даже реплика, обращённая
    к клиенту, в рассылке реакцией не становится.
    """
    body = (
        "Уважаемые клиенты, напоминаем: вам нужно сдать отчётность до 25-го "
        "числа. Все ли документы вы успели собрать?"
    )
    messages = [
        message(3343, seconds=msk(10, 0), author=CLIENT_A, text=BANK_UPLOAD_REQUEST),
        message(3484, COMPANY, seconds=msk(10, 20), text=IRINA + body),
    ]
    rows = tuple(
        (mid, chat, T0 + timedelta(seconds=msk(10, 20)), body)
        for mid, chat in ((3484, 1), (9001, 2), (9002, 3))
    )
    snapshot = batch(messages, {3343: REQUEST, 3484: COMPANY_OTHER},
                     broadcasts=rows).replay_input
    snapshot = replace(
        snapshot, attribution_map=MappingProxyType({3484: STAFF_IRINA}),
        rules_since=(T0 - timedelta(days=1),) * 3,
    )
    result = await rebuild_interactions(
        None, replay_input=snapshot, persist=False, now=T0 + timedelta(hours=6),
        settle_open=False,
    )
    items = {item.opened_by_message_id: item for item in result["items"]}
    assert result["broadcast_ids"] == [3484, 9001, 9002]
    assert items[3343].first_reaction_at is None


async def test_an_integrator_notice_never_becomes_a_reaction():
    """Уведомление интегратора отсеивается раньше, чем читается метка `other`."""
    from app.db.models import TransportActorKind

    messages = [
        message(3343, seconds=msk(10, 0), author=CLIENT_A, text=BANK_UPLOAD_REQUEST),
        message(3484, COMPANY, seconds=msk(10, 20), text="Вы не авторизованы",
                actor=TransportActorKind.INTEGRATOR_BOT),
    ]
    items, _ = await replay(messages, {3343: REQUEST, 3484: COMPANY_OTHER})
    assert items[3343].first_reaction_at is None
    assert items[3343].state is InteractionState.OPEN


async def test_an_existing_first_reaction_is_not_rewritten():
    """Как и `ack`: уже полученная первая реакция не переписывается."""
    messages = [
        message(3343, seconds=msk(10, 0), author=CLIENT_A, text=BANK_UPLOAD_REQUEST),
        message(3400, COMPANY, seconds=msk(10, 5), text=IRINA + "Принято"),
        message(3484, COMPANY, seconds=msk(10, 20),
                text=POLINA + QUESTION_TO_THE_CLIENT_BY_NAME),
    ]
    items, _ = await replay(messages, {3343: REQUEST, 3400: COMPANY_ACK,
                                       3484: COMPANY_OTHER},
                            staff={3400: STAFF_IRINA, 3484: STAFF_KSENIA})
    assert items[3343].first_reaction_message_id == 3400
    assert items[3343].first_reaction_staff_id == STAFF_IRINA


async def test_the_reaction_reaches_every_waiting_work_like_an_ack():
    """Реплика без reply — общая первая реакция всем ожидающим (R-19).

    До первой реакции каждая просьба открывает своё обращение (R-18), и
    отклик в чате — это отклик клиенту, а не одной строке переписки.
    """
    messages = [
        message(3343, seconds=msk(10, 0), author=CLIENT_A, text=BANK_UPLOAD_REQUEST),
        message(3350, seconds=msk(10, 2), author=CLIENT_B,
                text="И справку о численности подготовьте, пожалуйста"),
        message(3484, COMPANY, seconds=msk(10, 20),
                text=POLINA + QUESTION_TO_THE_CLIENT_BY_NAME),
    ]
    items, _ = await replay(messages, {3343: REQUEST, 3350: REQUEST,
                                       3484: COMPANY_OTHER},
                            staff={3484: STAFF_KSENIA})
    assert items[3343].first_reaction_message_id == 3484
    assert items[3350].first_reaction_message_id == 3484


async def test_a_work_where_nothing_needed_an_answer_stays_no_response_needed():
    """Реакция на обращение, где отвечать было не на что, ничего не меняет.

    `settle` судит по флагам клиента, а не по первой реакции: сигнала такое
    обращение не даёт по определению (`valid = state != no_response_needed`).
    """
    messages = [
        message(3343, seconds=msk(10, 0), author=CLIENT_A, text="Спасибо!"),
        message(3484, COMPANY, seconds=msk(10, 20),
                text=POLINA + QUESTION_TO_THE_CLIENT_BY_NAME),
    ]
    items, _ = await replay(messages, {3343: (False, None, "ack", None),
                                       3484: COMPANY_OTHER},
                            staff={3484: STAFF_KSENIA}, settle_open=True)
    assert items[3343].state is InteractionState.NO_RESPONSE_NEEDED


@pytest.mark.parametrize("text", [ANNOUNCE_CHAT_RESTORED, ANNOUNCE_CHAT_DELETION])
async def test_an_announcement_labelled_other_is_a_first_reaction(text):
    """Объявление с меткой `other` в первые 30 минут — тоже реакция.

    Адресованность текста движок не проверяет: решает метка.
    """
    items, _ = await _run(*_reply_case(text, client_text=SIGN_RECONCILIATION_REQUEST,
                                       signature=POLINA),
                          staff={3484: STAFF_KSENIA})
    assert items[3343].first_reaction_message_id == 3484
    assert items[3343].sla_breached is False


async def test_an_other_reaction_does_not_pass_for_a_substantive_answer():
    """Реакция по `other` ответом по существу не притворяется: ответ даёт
    следующая реплика `substantive`."""
    messages = [
        message(3343, seconds=msk(10, 0), author=CLIENT_A, text=BANK_UPLOAD_REQUEST),
        message(3484, COMPANY, seconds=msk(10, 20),
                text=POLINA + QUESTION_TO_THE_CLIENT_BY_NAME),
        message(3500, COMPANY, seconds=msk(11, 0),
                text=IRINA + "Платёжку выгрузили, проверьте в банке"),
    ]
    items, _ = await replay(messages, {3343: REQUEST, 3484: COMPANY_OTHER,
                                       3500: COMPANY_SUBSTANTIVE},
                            staff={3484: STAFF_KSENIA, 3500: STAFF_IRINA},
                            settle_open=True)
    assert items[3343].first_reaction_message_id == 3484
    assert items[3343].state is InteractionState.ANSWERED


# ── Окно ожидания ответа клиента ─────────────────────────────────────────
#
# Метку `other` читают две ветки. Окно «ждём клиента» такая реплика не
# взводит и не гасит; реакцией она становится только для открытых обращений.


async def test_an_other_reply_does_not_close_the_answer_window():
    """`other` между вопросом компании и ответом клиента окно ответа не гасит."""
    messages = [
        message(1, COMPANY, seconds=msk(9, 50),
                text=IRINA + "Уточните, счёт на ИП или на ООО?"),
        message(2, COMPANY, seconds=msk(10, 0),
                text=IRINA + "Коллеги, завтра офис не работает"),
        message(3, seconds=msk(10, 5), author=CLIENT_A,
                text="Сейчас посмотрю в договоре"),
    ]
    verdicts = {1: (None, False, "question", None), 2: COMPANY_OTHER, 3: REQUEST}
    _, result = await replay(messages, verdicts, staff={1: STAFF_IRINA, 2: STAFF_IRINA})
    assert 3 in result["answers"]
    assert 3 not in result["response_required_ids"]


async def test_an_other_reply_does_not_arm_the_answer_window():
    """«Как отпуск прошёл?» с меткой `other` окно ответа не взводит:
    следующая просьба клиента — новое обращение, а не ответ."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + "Как отпуск прошёл?"),
        message(2, seconds=msk(10, 5), author=CLIENT_A,
                text="Подготовьте акт сверки за август"),
    ]
    items, result = await replay(messages, {1: COMPANY_OTHER, 2: REQUEST},
                                 staff={1: STAFF_IRINA})
    assert 2 not in result["answers"]
    assert 2 in result["response_required_ids"]
    assert items[2].state is InteractionState.OPEN

