"""Второй слой (ожидание специалиста, правила v4): ссылка на соседа по пачке и отзыв просьбы.

Встречный вопрос со ссылкой на соседа по пачке — или результат по существу
другого специалиста — засчитывается единственному ожиданию специалиста.
Клиент, отозвавший просьбу при единственном ожидании специалиста, снимает
его. Метки, ссылки и авторство здесь — заданный вход, а не предсказание
модели.
"""

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

import pytest

from app.db.models import InteractionState
from app.services.episodes import rebuild_interactions
from tests.test_classify_context_v11 import COMPANY, REQUEST, T0, batch, message

# Вердикт: (requires_response, is_substantive, label, answers_request_id).
CLIENT_QUESTION = (True, None, "question", None)
CLIENT_ACK = (False, None, "ack", None)

# Подписи интегратора: сотрудники разные, а tg-аккаунт один (бот
# интегратора), поэтому авторство приходит только атрибуцией.
DARYA = "Дарья Лебедева [corp.example.com] пишет:\n\n"
KSENIA = "Ксения Морозова [corp.example.com] пишет:\n\n"
IRINA = "Ирина Соколова [corp.example.com] пишет:\n\n"

STAFF_DARYA, STAFF_KSENIA, STAFF_IRINA = 11, 22, 33


async def replay(messages, verdicts, *, staff=None, version=4, after=timedelta(hours=20)):
    """Хелпер `test_engine_v4_replay.replay` с настраиваемым `now`.

    Свой `now` нужен, когда между просьбой клиента и ответом специалиста
    лежат сутки. `settle_open=False`: финальный проход закрыл бы обращения
    по своим правилам и спрятал состояние второго слоя на момент реплики.
    """
    snapshot = batch(messages, verdicts).replay_input
    snapshot = replace(
        snapshot, attribution_map=MappingProxyType(staff or {}),
        rules_since=tuple(T0 - timedelta(days=1) if version >= v else None for v in (2, 3, 4)),
    )
    result = await rebuild_interactions(
        None, replay_input=snapshot, persist=False, now=T0 + after, settle_open=False,
    )
    return {item.opened_by_message_id: item for item in result["items"]}, result


# ── 1. Ссылка на соседа по пачке ─────────────────────────────────────────


def _case_sibling_question():
    """Три реплики клиента одной темы, передача — первая реакция всем трём,
    затем встречный вопрос другого специалиста со ссылкой на соседа.

    В первой реплике нет слов о звонке: просьба «только позвонить» второй
    слой не открывает, и проверять было бы нечего.
    """
    return [
        message(6481, seconds=0, author=700000003,
                text="Добрый день. Мы обратились в банк и, чтобы дать вам доступ, "
                     "нам сказали, чтобы наш бухгалтер дал свои данные "
                     "и написал ФИО"),
        message(6482, seconds=87, author=700000004,
                text="Привет, у наших бухгалтеров есть доступы.\n\n"
                     "Или здесь мешают какие-то ограничения?"),
        message(6484, seconds=964, author=700000003,
                text="Доступа нет, как я поняла"),
        message(6502, COMPANY, seconds=52559,
                text=DARYA + "Доброе утро. Передала бухгалтеру письмо банка."),
        message(6528, COMPANY, seconds=57557,
                text=KSENIA + "Доброе утро. Раз от банка трудно получить точную "
                             "информацию, нужно сами чеки проанализировать. "
                             "Сможете дать нам доступ в ваш личный кабинет "
                             "вашего оператора ФД (Эвотор)?"),
    ]


_SIBLING_QUESTION_VERDICTS = {
    6481: REQUEST,
    6482: CLIENT_QUESTION,
    6484: CLIENT_QUESTION,
    6502: (None, False, "handoff", 6481),
    6528: (None, False, "question", 6484),
}
_SIBLING_QUESTION_STAFF = {6502: STAFF_DARYA, 6528: STAFF_KSENIA}


async def test_counter_question_linked_to_a_batch_sibling_closes_the_wait():
    """Встречный вопрос со ссылкой на соседа зачтён ожидающему обращению."""
    messages = _case_sibling_question()
    items, _ = await replay(messages, _SIBLING_QUESTION_VERDICTS, staff=_SIBLING_QUESTION_STAFF)
    waiting = items[6481]
    assert waiting.handoff_at == messages[3].sent_at
    assert waiting.substantive_at == messages[4].sent_at
    assert waiting.substantive_breached is False
    # Встречный вопрос ожидание выполняет, но обращение не закрывает —
    # ход за клиентом, третьего слоя нет.
    assert waiting.state is InteractionState.REACTED
    # Связанное обращение обработано как прежде: ожидания у него не было,
    # второй слой ему и не открывался.
    assert items[6484].handoff_at is None and items[6484].substantive_at is None


async def test_link_to_a_sibling_from_another_batch_keeps_the_wait():
    """Пачка — это ОДНА реплика-первая-реакция на оба обращения.

    Здесь первые реакции разные: передача #3 пришла с Telegram-reply на #1
    и досталась только ему, а #2 впервые услышали лишь на #4.
    Соседство не доказано — ожидание по #1 сохраняется.
    """
    messages = [
        message(1, seconds=0, text="Нужен акт сверки за август"),
        message(2, seconds=30, text="И ещё вопрос по декларации"),
        message(3, COMPANY, seconds=120, reply_to=1,
                text=IRINA + "Передала бухгалтеру"),
        message(4, COMPANY, seconds=180, text=IRINA + "Добрый день, всё увидела"),
        message(5, COMPANY, seconds=600,
                text=KSENIA + "Подскажите, а за какой период нужна сверка?"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: CLIENT_QUESTION,
        3: (None, False, "handoff", 1),
        4: (None, False, "ack", None),
        5: (None, False, "question", 2),
    }, staff={3: STAFF_IRINA, 4: STAFF_IRINA, 5: STAFF_KSENIA})
    assert items[1].first_reaction_message_id == 3
    assert items[2].first_reaction_message_id == 4
    assert items[1].handoff_at == messages[2].sent_at
    assert items[1].substantive_at is None


async def test_two_open_specialist_waits_are_never_guessed():
    """При двух ожиданиях движок не выбирает за модель."""
    messages = [
        message(1, seconds=0, text="Нужен акт сверки за август"),
        message(2, seconds=30, text="И приглашение в банк"),
        message(3, COMPANY, seconds=120, text=IRINA + "Передала бухгалтеру"),
        message(4, COMPANY, seconds=180, text=IRINA + "И это тоже передала"),
        message(5, seconds=240, text="А ещё вопрос по декларации"),
        message(6, COMPANY, seconds=600,
                text=KSENIA + "Подскажите, а за какой период нужна сверка?"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: REQUEST, 5: CLIENT_QUESTION,
        3: (None, False, "handoff", 1),
        4: (None, False, "handoff", 2),
        6: (None, False, "question", 5),
    }, staff={3: STAFF_IRINA, 4: STAFF_IRINA, 6: STAFF_KSENIA})
    assert items[1].handoff_at is not None and items[2].handoff_at is not None
    assert items[1].substantive_at is None
    assert items[2].substantive_at is None


async def test_a_handoff_linked_to_a_sibling_does_not_close_the_wait():
    """Передача соседу — не ответ ожидающему, даже если она из той же пачки."""
    messages = [
        message(1, seconds=0, text="Нужен акт сверки за август"),
        message(2, seconds=30, text="И приглашение в банк"),
        message(3, COMPANY, seconds=120, text=IRINA + "Передала бухгалтеру"),
        message(4, COMPANY, seconds=600, text=KSENIA + "Это передала Вере"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: REQUEST,
        3: (None, False, "handoff", 1),
        4: (None, False, "handoff", 2),
    }, staff={3: STAFF_IRINA, 4: STAFF_KSENIA})
    assert items[1].handoff_at == messages[2].sent_at
    assert items[1].substantive_at is None


@pytest.mark.parametrize("label,substantive", [
    ("ack", False), ("promise", False), ("substantive", True),
])
async def test_the_handing_manager_reply_to_a_sibling_does_not_close_the_wait(
    label, substantive,
):
    """Собственная реплика передавшего менеджера соседу вторым слоем не считается.

    Результат соседу засчитывается только от другого специалиста, а «принято»
    и обещание — не встречный вопрос (знака вопроса и метки `question` нет).
    """
    messages = [
        message(1, seconds=0, text="Нужен акт сверки за август"),
        message(2, seconds=30, text="И приглашение в банк"),
        message(3, COMPANY, seconds=120, text=IRINA + "Передала бухгалтеру"),
        message(4, COMPANY, seconds=600, text=IRINA + "Принято, в работе"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: REQUEST,
        3: (None, False, "handoff", 1),
        4: (None, substantive, label, 2),
    }, staff={3: STAFF_IRINA, 4: STAFF_IRINA})
    assert items[1].substantive_at is None
    assert items[1].state is InteractionState.REACTED


@pytest.mark.parametrize("label", ["ack", "promise"])
async def test_another_specialist_ack_to_a_sibling_keeps_the_wait(label):
    """«Принято» или обещание другого специалиста по соседней просьбе ожидание не снимают.

    Соседу засчитываются только встречный вопрос и результат по существу.
    """
    messages = [
        message(1, seconds=0, text="Нужен акт сверки за август"),
        message(2, seconds=30, text="И приглашение в банк"),
        message(3, COMPANY, seconds=120, text=IRINA + "Передала бухгалтеру"),
        message(4, COMPANY, seconds=600, text=KSENIA + "Принято, в работе"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: REQUEST,
        3: (None, False, "handoff", 1),
        4: (None, False, label, 2),
    }, staff={3: STAFF_IRINA, 4: STAFF_KSENIA})
    assert items[1].substantive_at is None
    assert items[1].state is InteractionState.REACTED


async def test_another_specialist_result_for_a_sibling_closes_the_wait():
    """Результат по существу другого специалиста со ссылкой на соседа закрывает ожидание.

    Специалист вышел на связь с готовым результатом — ожидание выполнено,
    обращение отвечено.
    """
    messages = [
        message(1, seconds=0, text="Нужен акт сверки за август"),
        message(2, seconds=30, text="И приглашение в банк"),
        message(3, COMPANY, seconds=120, text=IRINA + "Передала бухгалтеру"),
        message(4, COMPANY, seconds=600, text=KSENIA + "Приглашение отправлено"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: REQUEST,
        3: (None, False, "handoff", 1),
        4: (None, True, "substantive", 2),
    }, staff={3: STAFF_IRINA, 4: STAFF_KSENIA})
    assert items[1].substantive_at == messages[3].sent_at
    assert items[1].substantive_staff_id == STAFF_KSENIA
    assert items[1].state is InteractionState.ANSWERED


async def test_an_unattributed_reply_to_a_sibling_keeps_the_wait():
    """Неизвестный автор выхода специалиста не доказывает."""
    messages = [
        message(1, seconds=0, text="Нужен акт сверки за август"),
        message(2, seconds=30, text="И приглашение в банк"),
        message(3, COMPANY, seconds=120, text=IRINA + "Передала бухгалтеру"),
        message(4, COMPANY, seconds=600, text="Принято, в работе"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: REQUEST,
        3: (None, False, "handoff", 1),
        4: (None, False, "ack", 2),
    }, staff={3: STAFF_IRINA})
    assert items[1].substantive_at is None


async def test_a_result_linked_to_the_waiting_work_itself_closes_it():
    """Адресный случай: ссылка пришла ровно туда, где ждут, — ветка соседа не нужна."""
    messages = [
        message(1, seconds=0, text="Нужен акт сверки за август"),
        message(2, COMPANY, seconds=120, text=IRINA + "Передала бухгалтеру"),
        message(3, COMPANY, seconds=600, text=KSENIA + "Сверка во вложении"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST,
        2: (None, False, "handoff", 1),
        3: (None, True, "substantive", 1),
    }, staff={2: STAFF_IRINA, 3: STAFF_KSENIA})
    assert items[1].substantive_at == messages[2].sent_at
    assert items[1].state is InteractionState.ANSWERED


# ── 2. Клиент отозвал просьбу ────────────────────────────────────────────


def _case_withdrawn_request():
    """Просьба → передача → отзыв просьбы клиентом → «принято» менеджера."""
    return [
        message(6711, seconds=0,
                text="Здравствуйте, а у Дмитрия номер заканчивается 0000? "
                     "Не могу дозвониться. Можете попросить её набрать меня?"),
        message(6716, COMPANY, seconds=95,
                text=IRINA + "Добрый день, передам ваш запрос"),
        message(6758, seconds=3082,
                text="Извините, уже не надо, вопрос решился сам, отбой, "
                     "не беспокойте Дмитрия)"),
        message(6762, COMPANY, seconds=3225, text=IRINA + "принято"),
    ]


_WITHDRAWN_REQUEST_VERDICTS = {
    6711: REQUEST,
    6716: (None, False, "handoff", 6711),
    6758: CLIENT_ACK,
    6762: (None, False, "ack", None),
}


async def test_client_withdrawing_the_request_closes_the_specialist_wait():
    """Клиент отозвал просьбу — ожидание специалиста снимается."""
    items, _ = await replay(_case_withdrawn_request(), _WITHDRAWN_REQUEST_VERDICTS,
                            staff={6716: STAFF_IRINA})
    withdrawn = items[6711]
    assert withdrawn.state is InteractionState.NO_RESPONSE_NEEDED
    # Ответ специалиста придумывать нельзя: ни времени, ни автора.
    assert withdrawn.substantive_at is None
    assert withdrawn.substantive_staff_id is None
    assert withdrawn.substantive_breached is not True
    # Первый слой не переписан: реакция была и была вовремя.
    assert withdrawn.first_reaction_message_id == 6716
    assert withdrawn.sla_breached is False


@pytest.mark.parametrize("text", [
    "Извините, не надо уже",
    "уже не надо, спасибо",
    "Отбой!",
    "вопрос решился сам собой",
    "вопрос решён, спасибо",          # ё→е: стебель «вопрос решен»
    "Вопрос снят",
    "уже не актуально",
    "мы сами разобрались",
    "Уже решили, не нужно",
])
async def test_every_withdrawal_stem_closes_the_wait(text):
    """Список стеблей — узкий и по смыслу отмены, а не по вежливости."""
    messages = [
        message(1, seconds=0, text="Попросите Дмитрия перезвонить мне"),
        message(2, COMPANY, seconds=95, text=IRINA + "Добрый день, передам ваш запрос"),
        message(3, seconds=3000, text=text),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: (None, False, "handoff", 1), 3: CLIENT_ACK,
    }, staff={2: STAFF_IRINA})
    assert items[1].state is InteractionState.NO_RESPONSE_NEEDED


@pytest.mark.parametrize("text", [
    "Спасибо!",
    "Хорошо, ждём",
    "Понятно",
    "Добрый день, а когда примерно ответит специалист",
])
async def test_polite_replies_do_not_close_the_wait(text):
    """«Спасибо» — это «услышал», а не «не делайте»: ожидание остаётся."""
    messages = [
        message(1, seconds=0, text="Попросите Дмитрия перезвонить мне"),
        message(2, COMPANY, seconds=95, text=IRINA + "Добрый день, передам ваш запрос"),
        message(3, seconds=3000, text=text),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: (None, False, "handoff", 1), 3: CLIENT_ACK,
    }, staff={2: STAFF_IRINA})
    assert items[1].state is InteractionState.REACTED
    assert items[1].substantive_at is None


async def test_a_withdrawal_plus_a_new_request_keeps_the_wait():
    """«Не надо уже, но пришлите акт» — метка `request`: новую просьбу не глушим.

    Реплика, открывающая обращение, отзывом не считается.
    """
    messages = [
        message(1, seconds=0, text="Попросите Дмитрия перезвонить мне"),
        message(2, COMPANY, seconds=95, text=IRINA + "Добрый день, передам ваш запрос"),
        message(3, seconds=3000,
                text="Звонок не надо уже, но пришлите, пожалуйста, акт сверки"),
    ]
    items, result = await replay(messages, {
        1: REQUEST, 2: (None, False, "handoff", 1), 3: REQUEST,
    }, staff={2: STAFF_IRINA})
    assert items[1].state is InteractionState.REACTED
    assert items[1].substantive_at is None
    assert 3 in result["response_required_ids"]


async def test_a_withdrawal_among_two_open_works_changes_nothing():
    """При двух открытых обращениях неизвестно, что именно отозвали."""
    messages = [
        message(1, seconds=0, text="Попросите Дмитрия перезвонить мне"),
        message(2, seconds=30, text="И пришлите акт сверки за август"),
        message(3, COMPANY, seconds=120, text=IRINA + "Добрый день, передам ваш запрос"),
        message(4, seconds=3000, text="Извините, не надо уже, отбой"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: REQUEST, 3: (None, False, "handoff", 1), 4: CLIENT_ACK,
    }, staff={3: STAFF_IRINA})
    assert items[1].state is InteractionState.REACTED
    assert items[1].substantive_at is None


async def test_withdrawal_after_the_first_reaction_deadline_keeps_the_silence():
    """Отзыв после срока первой реакции молчание компании не отменяет."""
    messages = [
        message(1, seconds=0, text="Попросите Дмитрия перезвонить мне"),
        message(2, seconds=3000, text="Извините, не надо уже, отбой"),
    ]
    items, _ = await replay(messages, {1: REQUEST, 2: CLIENT_ACK})
    assert items[1].first_reaction_at is None
    assert items[1].state is not InteractionState.NO_RESPONSE_NEEDED


async def test_a_withdrawal_before_any_handoff_changes_nothing():
    """Без открытого ожидания специалиста снимать нечего."""
    messages = [
        message(1, seconds=0, text="Попросите Дмитрия перезвонить мне"),
        message(2, COMPANY, seconds=95, text=IRINA + "Принято"),
        message(3, seconds=3000, text="Извините, не надо уже, отбой"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: (None, False, "ack", None), 3: CLIENT_ACK,
    }, staff={2: STAFF_IRINA})
    assert items[1].state is InteractionState.REACTED
    assert items[1].handoff_at is None


async def test_withdrawal_after_the_specialist_answered_changes_nothing():
    """Ожидание уже закрыто ответом специалиста — снимать нечего."""
    messages = [
        message(1, seconds=0, text="Попросите Дмитрия перезвонить мне"),
        message(2, COMPANY, seconds=95, text=IRINA + "Добрый день, передам ваш запрос"),
        message(3, COMPANY, seconds=600, text=KSENIA + "Дмитрий наберёт вас после обеда"),
        message(4, seconds=3000, text="Извините, не надо уже, отбой"),
    ]
    items, _ = await replay(messages, {
        1: REQUEST, 2: (None, False, "handoff", 1),
        3: (None, True, "substantive", 1), 4: CLIENT_ACK,
    }, staff={2: STAFF_IRINA, 3: STAFF_KSENIA})
    assert items[1].substantive_at == messages[2].sent_at
    assert items[1].state is InteractionState.ANSWERED


# ── 3. Правила действуют только в v4 ─────────────────────────────────────


@pytest.mark.parametrize("version", [2, 3])
async def test_older_rule_versions_do_not_close_on_a_withdrawal(version):
    """В v2/v3 отзыв просьбы клиентом ожидание специалиста не снимает."""
    items, _ = await replay(_case_withdrawn_request(), _WITHDRAWN_REQUEST_VERDICTS,
                            staff={6716: STAFF_IRINA}, version=version)
    assert items[6711].state is not InteractionState.NO_RESPONSE_NEEDED
    assert items[6711].handoff_at is not None
