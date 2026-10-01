"""Отзыв просьбы до первой реакции, реплика `other` другого сотрудника как выход специалиста
и передача без ссылки при единственной открытой работе (`v4_mode`).

Клиент, отозвавший свою просьбу до первой реакции и до её срока, ответа не ждёт. Реплика `other`
другого установленного сотрудника (не передавшего) при единственном ожидании специалиста —
выход специалиста на связь. Передача без ссылки открывает второй слой единственной открытой
работе, если передача — её первая реакция, в работе нет `offline` и просьба не только о звонке.
Метки, ссылки и авторство здесь — заданный вход, а не предсказание модели."""

from datetime import timedelta

import pytest

from app.db.models import InteractionState
from app.services.episodes import request_withdrawn_before_reaction
from tests.test_classify_context_v11 import COMPANY, REQUEST, message
from tests.test_engine_v4_sibling_and_withdrawal import (
    CLIENT_ACK, CLIENT_QUESTION, KSENIA, IRINA, STAFF_KSENIA, STAFF_IRINA, replay,
)

MIN = 60


# ── Отзыв просьбы до первой реакции ───────────────────────────────────────


@pytest.mark.parametrize("at,author,closed", [
    (25 * MIN, 7, True),
    (90 * MIN, 7, False),      # после срока реакции просрочка остаётся
    (25 * MIN, 8, False),      # отозвал не автор просьбы
])
async def test_a_withdrawal_before_the_first_reaction(at, author, closed):
    messages = [
        message(1, seconds=0, author=7, text="Завтра нужна помощь Андрея с УТМ"),
        message(2, seconds=at, author=author, text="С Андреем вопрос решили)))"),
    ]
    items, _ = await replay(messages, {1: REQUEST, 2: CLIENT_ACK})
    assert (items[1].state is InteractionState.NO_RESPONSE_NEEDED) is closed
    if closed:
        assert 2 not in items  # отзыв остаётся в закрытой работе, нового обращения нет


async def test_a_withdrawal_inside_the_first_reaction_deadline_needs_no_response():
    msgs = [message(1, seconds=0, author=7, text="Нужна помощь Андрея завтра"),
            message(2, seconds=10 * MIN, author=7, text="С Андреем вопрос решили")]
    items, _ = await replay(msgs, {1: REQUEST, 2: CLIENT_ACK}, after=timedelta(hours=5))
    assert items[1].state is InteractionState.NO_RESPONSE_NEEDED


async def test_a_withdrawal_after_the_first_reaction_deadline_keeps_the_breach():
    msgs = [message(1, seconds=0, author=7, text="Нужна помощь Андрея завтра"),
            message(2, seconds=50 * MIN, author=7, text="С Андреем вопрос решили")]
    items, _ = await replay(msgs, {1: REQUEST, 2: CLIENT_ACK}, after=timedelta(hours=5))
    assert items[1].state is not InteractionState.NO_RESPONSE_NEEDED
    assert items[1].first_reaction_at is None


async def test_a_withdrawal_after_a_reaction_does_not_rewrite_the_work():
    """После реакции работа уже не «без ответа» — отзыв её не переписывает."""
    msgs = [message(1, seconds=0, author=7, text="Нужна помощь Андрея завтра"),
            message(2, COMPANY, seconds=5 * MIN, text=IRINA + "Добрый день, уточню"),
            message(3, seconds=10 * MIN, author=7, text="С Андреем вопрос решили")]
    items, _ = await replay(msgs, {1: REQUEST, 2: (None, False, "ack", None), 3: CLIENT_ACK},
                            staff={2: STAFF_IRINA}, after=timedelta(hours=5))
    assert items[1].first_reaction_message_id == 2
    assert items[1].state is not InteractionState.NO_RESPONSE_NEEDED


async def test_a_withdrawal_does_not_guess_between_two_open_works():
    msgs = [message(1, seconds=0, author=7, text="Нужна помощь Андрея завтра"),
            message(2, seconds=2 * MIN, author=8, text="И пришлите акт сверки"),
            message(3, seconds=10 * MIN, author=7, text="С Андреем вопрос решили")]
    items, _ = await replay(msgs, {1: REQUEST, 2: REQUEST, 3: CLIENT_ACK}, after=timedelta(hours=5))
    assert items[1].state is not InteractionState.NO_RESPONSE_NEEDED


@pytest.mark.parametrize("text,expected", [
    ("С Андреем вопрос решили", True),
    ("уже не требуется, спасибо", True),
    ("Отбой", True),
    ("Спасибо, понятно", False),
    ("Решили оплатить завтра", False),
])
def test_a_withdrawal_is_recognised_by_its_text(text, expected):
    assert request_withdrawn_before_reaction(text) is expected


# ── `other` другого сотрудника при единственном ожидании специалиста ──────


def _other_case(author_staff):
    messages = [
        message(1, seconds=0, text="Что с ИП?"),
        message(2, COMPANY, seconds=MIN, text=IRINA + "Передала ваш запрос"),
        message(3, COMPANY, seconds=20 * MIN, text=KSENIA + "Сегодня сдам отчётность"),
    ]
    verdicts = {1: CLIENT_QUESTION, 2: (None, False, "handoff", 1), 3: (None, False, "other", None)}
    staff = {2: STAFF_IRINA}
    if author_staff is not None:
        staff[3] = author_staff
    return messages, verdicts, staff


@pytest.mark.parametrize("author,closes", [
    (STAFF_KSENIA, True),
    (STAFF_IRINA, False),      # передавший менеджер специалистом не становится
    (None, False),            # автор не установлен
])
async def test_an_other_reply_of_another_staff_member_is_the_specialist_contact(author, closes):
    messages, verdicts, staff = _other_case(author)
    items, _ = await replay(messages, verdicts, staff=staff)
    assert (items[1].substantive_message_id == 3) is closes
    if closes:
        assert items[1].state is InteractionState.ANSWERED
        assert items[1].substantive_staff_id == STAFF_KSENIA


async def test_an_other_reply_does_not_guess_between_two_specialist_waits():
    messages = [
        message(1, seconds=0, text="Что с ИП?"),
        message(2, seconds=30, text="И когда будет сверка?"),
        message(3, COMPANY, seconds=MIN, text=IRINA + "Передала ваш запрос"),
        message(4, COMPANY, seconds=MIN + 5, text=IRINA + "Передала ваш запрос"),
        message(5, COMPANY, seconds=20 * MIN, text=KSENIA + "Сегодня сдам отчётность"),
    ]
    verdicts = {1: CLIENT_QUESTION, 2: CLIENT_QUESTION, 3: (None, False, "handoff", 1),
                4: (None, False, "handoff", 2), 5: (None, False, "other", None)}
    items, _ = await replay(messages, verdicts, staff={3: STAFF_IRINA, 4: STAFF_IRINA, 5: STAFF_KSENIA})
    assert items[1].substantive_at is None and items[2].substantive_at is None


async def test_an_other_reply_linked_to_another_message_is_not_the_specialist_contact():
    messages, verdicts, staff = _other_case(STAFF_KSENIA)
    verdicts[3] = (None, False, "other", 999)
    items, _ = await replay(messages, verdicts, staff=staff)
    assert items[1].substantive_at is None


# ── Передача без ссылки при единственной открытой работе ──────────────────

HANDOFF_NO_LINK = (None, False, "handoff", None)


def _handoff_case(*, reacted_first=False, second_work=False, request=REQUEST, text="Пришлите акт сверки"):
    messages = [message(1, seconds=0, author=7, text=text)]
    verdicts = {1: request}
    staff = {}
    if second_work:
        messages.append(message(2, seconds=30, author=8, text="И счёт за сентябрь"))
        verdicts[2] = REQUEST
    if reacted_first:
        messages.append(message(3, COMPANY, seconds=2 * MIN, text=IRINA + "Добрый день, посмотрю"))
        verdicts[3] = (None, False, "ack", None)
        staff[3] = STAFF_IRINA
    messages.append(message(4, COMPANY, seconds=5 * MIN, text=IRINA + "Передала бухгалтеру"))
    verdicts[4] = HANDOFF_NO_LINK
    staff[4] = STAFF_IRINA
    return messages, verdicts, staff


async def test_a_handoff_without_link_opens_the_second_layer_of_the_only_work():
    messages, verdicts, staff = _handoff_case()
    items, _ = await replay(messages, verdicts, staff=staff)
    assert items[1].first_reaction_message_id == 4
    assert items[1].handoff_at is not None
    assert items[1].handoff_staff_id == STAFF_IRINA


async def test_a_handoff_after_an_earlier_reaction_opens_no_second_layer():
    """После уже данной реакции передача без ссылки может быть о другом."""
    messages, verdicts, staff = _handoff_case(reacted_first=True)
    items, _ = await replay(messages, verdicts, staff=staff)
    assert items[1].first_reaction_message_id == 3
    assert items[1].handoff_at is None


async def test_a_handoff_without_link_does_not_guess_between_two_works():
    messages, verdicts, staff = _handoff_case(second_work=True)
    items, _ = await replay(messages, verdicts, staff=staff)
    assert items[1].handoff_at is None and items[2].handoff_at is None


async def test_a_handoff_without_link_skips_a_work_with_an_offline_reply():
    """`offline` в составе — ответ вне чата: второй слой не открывается, даже когда в работе
    есть и другая просьба (правило «только звонок» такую работу не снимает)."""
    messages, verdicts, staff = _handoff_case()
    messages.insert(1, message(2, seconds=MIN, author=7, text="Можно и по телефону обсудить"))
    verdicts[2] = (False, None, "offline", None)
    items, _ = await replay(messages, verdicts, staff=staff)
    assert items[1].first_reaction_message_id == 4
    assert items[1].handoff_at is None


async def test_a_handoff_without_link_on_a_call_request_opens_no_second_layer():
    """Просьба только позвонить с меткой `request` второй слой не открывает."""
    messages, verdicts, staff = _handoff_case(text="Перезвоните мне, пожалуйста")
    items, _ = await replay(messages, verdicts, staff=staff)
    assert items[1].first_reaction_message_id == 4
    assert items[1].handoff_at is None


async def test_a_handoff_with_a_link_past_the_open_works_opens_no_second_layer():
    messages, verdicts, staff = _handoff_case()
    verdicts[4] = (None, False, "handoff", 999)
    items, _ = await replay(messages, verdicts, staff=staff)
    assert items[1].handoff_at is None


async def test_the_specialist_reply_closes_a_second_layer_opened_without_link():
    messages, verdicts, staff = _handoff_case()
    messages.append(message(5, COMPANY, seconds=40 * MIN, text=KSENIA + "Акт сверки во вложении"))
    verdicts[5] = (None, True, "substantive", None)
    staff[5] = STAFF_KSENIA
    items, _ = await replay(messages, verdicts, staff=staff)
    assert items[1].handoff_at is not None
    assert items[1].substantive_message_id == 5
    assert items[1].substantive_breached is False
    assert items[1].state is InteractionState.ANSWERED
