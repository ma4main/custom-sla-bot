"""Подтверждение оплаты, отзыв просьбы после срока специалиста и просьба позвонить (`v4_mode`).

Чистое подтверждение оплаты с меткой `request` обращения не открывает (платёжный файл того же
автора рядом — часть подтверждения). Отзыв просьбы снимает ожидание специалиста только до его
срока. По просьбе только позвонить, связаться или соединить второй слой не открывается. Метки,
ссылки и авторство здесь — заданный вход, а не предсказание модели."""

from datetime import timedelta

import pytest

from app.db.models import InteractionState
from app.services import episodes
from app.services.episodes import call_only_request, payment_file, pure_payment_confirmation
from tests.test_classify_context_v11 import COMPANY, REQUEST, message
from tests.test_engine_v4_sibling_and_withdrawal import CLIENT_ACK, IRINA, STAFF_IRINA, replay

HANDOFF_TO_FIRST = (None, False, "handoff", 1)
MIN = 60


# ── Словарь чистого подтверждения оплаты ──────────────────────────────────
#
# Чистое подтверждение: факт оплаты без побудительного глагола, без «?» и без просьбы.
# Короткое «готово/подписала» — только в ответ на просьбу компании оплатить или подписать.

PURE_CONFIRMATIONS = [
    ("Спасибо, оплатила", None),
    ("оплачено", None),
    ("Все оплатил. Спасибо.", None),
    ("Восстановление оплачено", None),
    ("восстановление оплачено", None),
    ("Оплатила", None),
    ("Оплачено, спасибо", None),
    ("Добрый день! \nОплатила. Спасибо!", None),
    ("Валентина,добрый день! \nОплатили", None),
    ("Эдо оплатил", None),
    ("Добрый день, счет оплачен", None),
    ("Добрый день, оплату внесла во вторник", None),
    ("Добрый день! Оплата аренды Петрову проведена.", None),
    ("🤖 Спасибо! Оплата прошла успешно. Чек: https://cheques.example.com/0001/abcd", None),
    ("Готово", "Добрый день! ПП на взносы в банке. Нужно подписать."),
    ("Готово", "Просьба оплатить платежку"),
    ("Подписала", "В банке ведомость на аванс, необходимо подписать"),
    ("поняла, благодарю, платёж подписали", None),
]

STILL_REQUESTS = [
    ("Оплатили, пришлите закрывающие", None),
    ("оплатили?", None),
    ("как оплатить?", None),
    ("пришлите счёт на оплату", None),
    ("Пришлите счет на оплату", None),
    ("Оплатили, выставьте счёт на следующий месяц", None),
    ("Оплатила, закрывающие пожалуйста", None),
    ("Оплатили, когда будут закрывающие", None),
    ("Я оплатил за ИП Смирнова жду", None),
    ("Оплатили, ждём акт", None),
    ("Договора пока нет они просят чтобы по реквизитам оплатили", None),
    ("Не оплачен счет", None),
    ("Здравствуйте \nНе оплачен счет", None),
    ("не оплатила еще", None),
    ("Оплатили, но платёж вернулся", None),
    ("Оплатила, проверьте поступление", None),
    ("Оплатила, дайте знать", None),
    ("Оплатил. Скажите, а если я оплачиваю счет контракта самостоятельно, то вам надо сам счет?", None),
    ("Оплатили, нужно закрыть сделку в ЭДО", None),
    ("Оплатили, можно отгружать", None),
    ("Готово", "Пришлите, пожалуйста, фото документа"),
    ("Готово", None),
    ("Оплатить товар со счета на сумму 450 000 рублей.", None),
    ("Восстановление согласовали, оплатим сегодня", None),
    ("За июнь взносы я вроде бы все оплатила...", None),
    ("Упс. Он же на счёт оплатил.", None),
    ("Не оплачено / крайне важно оплатить сегодня", None),
]


@pytest.mark.parametrize("text,company", PURE_CONFIRMATIONS)
def test_a_pure_payment_confirmation_is_recognised(text, company):
    assert pure_payment_confirmation(text, company)


@pytest.mark.parametrize("text,company", STILL_REQUESTS)
def test_a_payment_with_a_request_or_a_question_stays_a_request(text, company):
    assert not pure_payment_confirmation(text, company)


def test_a_long_payment_message_is_not_read_as_a_pure_confirmation():
    """Длинный текст — уже рассказ, а не отметка «оплачено»: его судит модель."""
    text = "Оплатила. " + "Сегодня утром всё провели через банк, всё прошло штатно. " * 3
    assert len(text) > episodes.PURE_PAYMENT_MAX_LEN
    assert not pure_payment_confirmation(text)


@pytest.mark.parametrize("text,bare,expected", [
    ("", True, True),
    ("Платежное_поручение_49.pdf", False, True),
    ("чек.jpg", False, True),
    ("Договор_аренды.pdf", False, False),
    ("Платёжка во вложении, проверьте", False, False),
])
def test_a_payment_file_is_a_bare_attachment_or_a_payment_file_name(text, bare, expected):
    assert payment_file(text, bare) is expected


# ── Подтверждение оплаты в движке ─────────────────────────────────────────


async def _required(msgs, verdicts):
    """Какие сообщения движок считает требующими ответа."""
    _, result = await replay(msgs, verdicts, after=timedelta(hours=3))
    return set(result["response_required_ids"])


async def test_a_pure_payment_confirmation_labelled_request_opens_no_work():
    msgs = [message(1, seconds=0, author=7, text="Восстановление оплачено")]
    assert await _required(msgs, {1: REQUEST}) == set()


@pytest.mark.parametrize("text", ["Оплатили, пришлите закрывающие", "оплатили?", "как оплатить?",
                                  "пришлите счёт на оплату"])
async def test_a_payment_with_a_request_still_opens_a_work(text):
    assert await _required([message(1, seconds=0, author=7, text=text)], {1: REQUEST}) == {1}


def _file_then_paid(author_file=7, name="Платежное_поручение_49.pdf"):
    return [message(1, seconds=0, author=author_file, media="document", text=name),
            message(2, seconds=8, author=7, text="Оплатила")]


async def test_a_payment_file_followed_by_paid_is_one_confirmation():
    assert await _required(_file_then_paid(), {1: REQUEST, 2: REQUEST}) == set()


async def test_a_file_of_another_author_or_not_a_payment_keeps_its_own_work():
    assert await _required(_file_then_paid(author_file=8), {1: REQUEST, 2: REQUEST}) == {1}
    assert await _required(_file_then_paid(name="Договор_аренды.pdf"), {1: REQUEST, 2: REQUEST}) == {1}


async def test_a_bare_file_after_a_pure_confirmation_is_part_of_it():
    msgs = [message(1, seconds=0, author=7, text="Оплатила"),
            message(2, seconds=20, author=7, media="document", text=None)]
    assert await _required(msgs, {1: REQUEST}) == set()


async def test_a_short_done_after_a_company_payment_ask_is_a_confirmation():
    """«Готово» в ответ на «ПП в банке, нужно подписать» — подтверждение, а не обращение."""
    msgs = [message(1, COMPANY, seconds=0, text=IRINA + "Добрый день! ПП на взносы в банке. Нужно подписать."),
            message(2, seconds=10 * MIN, author=7, text="Готово")]
    verdicts = {1: (None, True, "substantive", None), 2: REQUEST}
    _, result = await replay(msgs, verdicts, staff={1: STAFF_IRINA}, after=timedelta(hours=3))
    assert 2 not in result["response_required_ids"]


# ── Отзыв просьбы и срок специалиста ──────────────────────────────────────


def _late_withdrawal(withdrawn_at=26 * 3600):
    return [
        message(1, seconds=0, text="Подготовьте, пожалуйста, справку 2-НДФЛ"),
        message(2, COMPANY, seconds=95, text=IRINA + "Добрый день, передала запрос бухгалтеру"),
        # По умолчанию — через сутки с лишним, позже срока специалиста
        # (то же время следующего рабочего дня).
        message(3, seconds=withdrawn_at, text="Извините, не надо уже, отбой"),
    ]


_LATE_WITHDRAWAL_VERDICTS = {1: REQUEST, 2: HANDOFF_TO_FIRST, 3: CLIENT_ACK}


async def test_a_withdrawal_after_the_specialist_deadline_keeps_the_breach():
    items, _ = await replay(_late_withdrawal(), _LATE_WITHDRAWAL_VERDICTS, staff={2: STAFF_IRINA},
                            after=timedelta(days=2))
    assert items[1].state is not InteractionState.NO_RESPONSE_NEEDED
    assert items[1].handoff_at is not None and items[1].substantive_at is None


async def test_a_withdrawal_before_the_specialist_deadline_clears_the_wait():
    items, _ = await replay(_late_withdrawal(withdrawn_at=3082), _LATE_WITHDRAWAL_VERDICTS,
                            staff={2: STAFF_IRINA}, after=timedelta(days=2))
    assert items[1].state is InteractionState.NO_RESPONSE_NEEDED


# ── Просьба позвонить: второй слой не открывается ─────────────────────────


def _call_case(text):
    return [message(1, seconds=0, text=text),
            message(2, COMPANY, seconds=2 * MIN, text=IRINA + "Передала запрос бухгалтеру")]


@pytest.mark.parametrize("text", ["Можете позвонить по начислению налогов Ивановой",
                                  "Соедините с Ниной", "Перезвоните мне, пожалуйста"])
async def test_a_call_request_gets_a_first_reaction_but_no_second_layer(text):
    items, _ = await replay(_call_case(text), {1: REQUEST, 2: HANDOFF_TO_FIRST}, staff={2: STAFF_IRINA})
    assert items[1].first_reaction_message_id == 2
    assert items[1].handoff_at is None


async def test_a_call_request_with_another_request_keeps_the_second_layer():
    text = "Руководителю необходимо созвониться. Пришлите прайс и копию договора"
    items, _ = await replay(_call_case(text), {1: REQUEST, 2: HANDOFF_TO_FIRST}, staff={2: STAFF_IRINA})
    assert items[1].handoff_at is not None


async def test_an_offline_call_request_opens_no_second_layer():
    """Метка `offline` с прямой просьбой связаться — тоже звонок."""
    msgs = _call_case("Можете со мной созвониться? Вопрос про УСН")
    items, _ = await replay(msgs, {1: (False, None, "offline", None), 2: HANDOFF_TO_FIRST},
                            staff={2: STAFF_IRINA})
    assert items[1].handoff_at is None


@pytest.mark.parametrize("text,expected", [
    ("Перезвоните мне, пожалуйста", True),
    ("Соедините с бухгалтером", True),
    ("Свяжитесь со мной", True),
    ("Созвонимся и пришлите акт", False),
    ("Позвоните и подготовьте справку", False),
    ("Пришлите акт сверки", False),
])
def test_a_call_only_request_is_recognised_by_its_text(text, expected):
    assert call_only_request(text) is expected
