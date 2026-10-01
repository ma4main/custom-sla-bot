"""Продолжение своей речи в правилах v4: поправка или дополнение клиента к собственной
только что закрытой работе не открывает нового дела со своим сроком.

Своей открытой работы нет, а своя закрылась не позже `CONTINUATION_WINDOW` (10 минут от
последнего слова клиента в ней) и не ответом по существу — реплика с меткой `correction`
или `addition` продолжает эту речь. Поправка к своей открытой работе, настоящая новая
просьба, поправка другого сотрудника клиента и поправка спустя полчаса ведут себя как
обычно.
"""

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

from app.db.models import InteractionState
from app.services.episodes import rebuild_interactions
from tests.test_classify_context_v11 import COMPANY, REQUEST, T0, batch, message

# Вердикт: (requires_response, is_substantive, label, answers_request_id).
CLIENT_ACK = (False, None, "ack", None)
CLIENT_CORRECTION = (False, None, "correction", None)
CLIENT_ADDITION = (False, None, "addition", None)
COMPANY_QUESTION = (None, False, "question", None)
COMPANY_SUBSTANTIVE = (None, True, "substantive", None)
COMPANY_PROMISE = (None, False, "promise", None)

DARYA = "Дарья Лебедева [corp.example.com] пишет:\n\n"
POLINA = "Полина Орлова [corp.example.com] пишет:\n\n"

# Два сотрудника со стороны клиента в одном чате.
CLIENT_A, CLIENT_B = 7000000001, 7000000002


async def replay(messages, verdicts, *, staff=None, after=timedelta(hours=6)):
    """Пересчёт без записи в правилах v4 с настраиваемым `now`.

    `settle_open=False`: финальный проход закрыл бы обращения по своим
    правилам и спрятал то, что проверяется, — состояние на момент реплики.
    """
    snapshot = batch(messages, verdicts).replay_input
    snapshot = replace(
        snapshot, attribution_map=MappingProxyType(staff or {}),
        rules_since=(T0 - timedelta(days=1),) * 3,
    )
    result = await rebuild_interactions(
        None, replay_input=snapshot, persist=False, now=T0 + after, settle_open=False,
    )
    return {item.opened_by_message_id: item for item in result["items"]}, result


# ── 1. Поправка после закрытой работы ────────────────────────────────────


def _case_late_correction():
    """Смещения в секундах от вопроса компании #6972."""
    return [
        message(6972, COMPANY, seconds=0,
                text=DARYA + "Доброе утро. У вас получится войти "
                                "в Диадок, подписать и отправить акты?"),
        message(6973, seconds=39, author=CLIENT_A, text="Добрый день, да"),
        message(6975, seconds=525, author=CLIENT_B, text="Альфа нет ЭЦП"),
        message(6976, COMPANY, seconds=541,
                text=DARYA + "Пожалуйста, зайдите в раздел Документы -> "
                                "черновики, подпишите и отправьте все УПД."),
        message(6977, COMPANY, seconds=542, media="document",
                text=DARYA + "делится файлом"),
        message(6978, COMPANY, seconds=596,
                text=DARYA + "Принято, уточню у коллеги"),
        message(6979, seconds=596, author=CLIENT_A, text="Точно, ЭЦП отозвана"),
        message(7030, COMPANY, seconds=5525,
                text=POLINA + "Добрый день. Тогда так, ставлю себе в план."),
    ]


_LATE_CORRECTION_VERDICTS = {
    6972: COMPANY_QUESTION,
    6973: CLIENT_ACK,
    6975: REQUEST,
    6976: COMPANY_SUBSTANTIVE,
    6977: COMPANY_SUBSTANTIVE,
    6978: COMPANY_PROMISE,
    6979: CLIENT_CORRECTION,
    7030: (None, True, "substantive", 6979),
}


async def test_a_correction_to_a_just_closed_own_work_opens_no_deadline():
    """Поправка к своей работе, закрытой минуты назад без ответа по существу, — не новое дело.

    Работа 6973 (в неё подшилась просьба 6975) закрылась «ответа не требовалось»;
    поправка 6979 того же автора ведёт себя как `ack`: обращение из одних ответов
    заводится и закрывается так же, срока и просрочки у него нет.
    """
    items, result = await replay(_case_late_correction(), _LATE_CORRECTION_VERDICTS)
    assert 6975 not in items
    assert items[6973].state is InteractionState.NO_RESPONSE_NEEDED
    assert 6979 not in result["response_required_ids"]
    assert items[6979].state is InteractionState.NO_RESPONSE_NEEDED
    assert items[6979].first_reaction_at is None
    assert items[6979].sla_breached is None


async def test_an_addition_to_a_just_closed_own_work_opens_no_deadline():
    """Метка `addition` продолжает свою речь так же, как `correction`."""
    messages = [
        message(1, seconds=0, author=CLIENT_A, text="Да, всё верно"),
        message(2, COMPANY, seconds=60, text=DARYA + "Принято"),
        message(3, seconds=120, author=CLIENT_A, text="И ещё по второму ИП то же самое"),
    ]
    verdicts = {1: CLIENT_ACK, 2: (None, False, "ack", None), 3: CLIENT_ADDITION}
    _, result = await replay(messages, verdicts)
    assert 3 not in result["response_required_ids"]


# ── 2. Контр-примеры: что правило трогать не должно ──────────────────────


async def test_correction_to_an_open_own_work_still_moves_the_deadline():
    """Поправка к СВОЕЙ ОТКРЫТОЙ работе до первой реакции переносит начало срока —
    отвечать надо на исправленные данные.
    """
    messages = [
        message(1, seconds=0, author=CLIENT_A,
                text="Подготовьте счёт на 12 000 рублей"),
        message(2, seconds=120, author=CLIENT_A, text="Извините, на 21 000"),
        message(3, COMPANY, seconds=600, text=DARYA + "Принято"),
    ]
    items, _ = await replay(messages, {1: REQUEST, 2: CLIENT_CORRECTION,
                                       3: (None, False, "ack", None)})
    work = items[1]
    assert work.opened_at == messages[1].sent_at
    assert work.first_reaction_message_id == 3


async def test_a_real_new_request_after_a_closed_work_still_opens_its_own():
    """Настоящая новая просьба с меткой `request` молчать не начинает.

    Работа клиента закрылась, и через минуту он пишет снова, но метка —
    `request`: продолжением своей речи бывают только `correction`/`addition`.
    """
    messages = [
        message(1, seconds=0, author=CLIENT_A, text="Спасибо, всё получил"),
        message(2, COMPANY, seconds=60, text=DARYA + "Рады помочь"),
        message(3, seconds=120, author=CLIENT_A,
                text="Подготовьте, пожалуйста, акт сверки за август"),
    ]
    items, result = await replay(messages, {1: CLIENT_ACK,
                                            2: (None, False, "ack", None),
                                            3: REQUEST})
    assert 3 in result["response_required_ids"]
    assert items[3].state is InteractionState.OPEN
    assert items[3].first_reaction_at is None


async def test_a_correction_by_another_client_employee_still_opens_its_own_work():
    """Поправка ДРУГОГО сотрудника клиента — своё дело со своим сроком.

    «Своя» работа ищется тем же `own_client_work`: в закрытой работе этот
    человек не писал, поэтому сигнал остаётся.
    """
    messages = [
        message(1550, seconds=0, author=CLIENT_A,
                text="Просьба все договоры и акты отправить сюда на проверку"),
        message(1551, COMPANY, seconds=40, text=DARYA + "принято в работу"),
        message(1552, seconds=181, author=CLIENT_B,
                text="Просьба поправить в договоре место проведения"),
    ]
    items, result = await replay(messages, {1550: REQUEST,
                                            1551: (None, False, "ack", None),
                                            1552: CLIENT_CORRECTION})
    assert 1552 in result["response_required_ids"]
    assert items[1552].first_reaction_at is None


async def test_a_correction_long_after_the_closed_work_still_opens_its_own():
    """«Только что» — это десять минут, а не «когда-нибудь раньше».

    Поправка того же автора через полчаса — уже не продолжение своей речи,
    а новое дело.
    """
    messages = [
        message(1, seconds=0, author=CLIENT_A, text="Да, всё верно"),
        message(2, COMPANY, seconds=60, text=DARYA + "Принято"),
        message(3, seconds=1860, author=CLIENT_A, text="Точно, ЭЦП отозвана"),
    ]
    items, result = await replay(messages, {1: CLIENT_ACK,
                                            2: (None, False, "ack", None),
                                            3: CLIENT_CORRECTION})
    assert 3 in result["response_required_ids"]
    assert items[3].first_reaction_at is None
