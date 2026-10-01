"""Окно ответа (только v4): ответ клиента на вопрос компании не открывает обращение.

Внутри окна (после вопроса компании, до того же времени следующего рабочего
дня) метки `request` и `question` обращения не открывают и срока не запускают.
Окно вскрывают: реплика с «?» или просьбой о звонке, `mixed_request` —
отдельное поручение внутри ответа, файл без подписи при закрытых работах.
Короткое подтверждение («Да.») окно не расходует. Окно взводит только
вопрос компании, не передача специалисту. Метки и ссылки — заданный вход,
а не предсказание модели.
"""

from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType

import pytest

from app.services.episodes import rebuild_interactions
from tests.test_classify_context_v11 import COMPANY, REQUEST, T0, batch, message

QUESTION = (True, None, "question", None)
MIXED = (True, None, "mixed_request", None)
HANDOFF = "handoff"


async def replay(messages, verdicts, *, version=4, after=timedelta(minutes=10)):
    """Копия хелпера `test_engine_v4_replay.replay` с настраиваемым `now`.

    Свой `now` нужен сценарию «через сутки после вопроса»: сообщение приходит
    позже, чем T0+10 минут исходного хелпера.
    """
    snapshot = batch(messages, verdicts).replay_input
    snapshot = replace(
        snapshot, attribution_map=MappingProxyType({}),
        rules_since=tuple(T0 - timedelta(days=1) if version >= v else None for v in (2, 3, 4)),
    )
    result = await rebuild_interactions(
        None, replay_input=snapshot, persist=False, now=T0 + after, settle_open=False,
    )
    return {item.opened_by_message_id: item for item in result["items"]}, result


# ── Ответ клиента внутри окна ────────────────────────────────────────────

@pytest.mark.parametrize("text,label", [
    ("Завтра внесу после обеда", "request"),
    ("Уточню", "request"),
    ("уточняю", "request"),
    ("В обработке еще пишут", "question"),
    ("Пока не могу отправить, чуть позже", "request"),
])
async def test_client_answer_inside_the_window_starts_no_deadline(text, label):
    """Обещание и уточнение в ответ на вопрос компании — не обращение."""
    messages = [message(1, COMPANY, text="Когда внесёте оплату за август?"),
                message(2, text=text)]
    items, result = await replay(messages, {1: (None, False, "question", None),
                                            2: (True, None, label, None)})
    assert 2 not in result["response_required_ids"]
    assert not any(item.sla_breached for item in items.values())


async def test_requested_document_with_a_greeting_starts_no_deadline():
    """Документ, присланный по прямой просьбе специалиста, — ответ, а не обращение.

    Подпись «Здравствуйте!» делает сообщение НЕ голым вложением, поэтому
    защита серии ответа его не покрывает — срок не запускает именно окно.
    """
    messages = [message(2580, COMPANY, seconds=0, text="Пришлите, пожалуйста, выписку по счёту"),
                message(2587, seconds=600, text="Здравствуйте!", media="document")]
    items, result = await replay(messages, {2580: (None, False, "question", None), 2587: REQUEST})
    assert 2587 not in result["response_required_ids"]
    assert not any(item.sla_breached for item in items.values())


# ── Содержание реплики вскрывает окно ────────────────────────────────────

@pytest.mark.parametrize("text,opens", [
    ("Уточню", False),
    ("А когда будет готово?", True),                    # встречный вопрос клиента
    ("Можете созвониться со мной?", True),              # просьба о звонке
    ("Позвоните мне после обеда", True),                # «позвон» без знака вопроса
    ("Перезвоните мне после обеда", True),              # «перезвон» — та же просьба
    ("Наберите меня завтра", True),                     # «набер» без знака вопроса
    ("Не могу дозвониться до банка", False),            # жалоба, не просьба
])
async def test_question_mark_or_call_request_opens_a_work_inside_the_window(text, opens):
    """«?» и стеммы `позвон`/`перезвон`/`созвон`/`набер` вскрывают окно — и только они.

    «дозвониться» намеренно НЕ маркер: это жалоба, а не просьба о звонке.
    """
    messages = [message(1, COMPANY, text="Когда внесёте оплату за август?"),
                message(2, text=text)]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: REQUEST})
    assert (2 in result["response_required_ids"]) is opens


@pytest.mark.parametrize("text", [
    "Жду звонка по ЭТрН",
    "Буду ждать звонка во вторник",
    "Жду звонки по обеим заявкам",
])
async def test_call_request_without_a_question_mark_opens_a_work(text):
    """«Жду звонка» — обращение, хотя знака вопроса в нём нет.

    Реплика про ожидаемый звонок требует реакции в срок первой реакции.
    Стебель «звонк» ловит «звонка»/«звонки»/«звонком». Стеблей «звон»
    и «звони» в списке нет: оба ловят «не могу дозвониться» — см. соседний тест.
    """
    messages = [message(1, COMPANY, text="Какой у вас номер телефона?"),
                message(2, text=text)]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: REQUEST})
    assert 2 in result["response_required_ids"]


@pytest.mark.parametrize("text", [
    "Не могу дозвониться до банка",       # жалоба, не просьба
    "Дозвонились наконец, всё выяснили",
])
async def test_the_dozvonitsya_family_stays_outside_the_stems(text):
    """Граница стебля «звонк»: «дозвониться» — это «звони», а не «звонк».

    Тест сторожит список: расширение до «звон»/«звони» превратило бы жалобу
    «не могу дозвониться» в обращение.
    """
    messages = [message(1, COMPANY, text="Какой у вас номер телефона?"),
                message(2, text=text)]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: REQUEST})
    assert 2 not in result["response_required_ids"]


async def test_call_request_markers_are_case_insensitive():
    messages = [message(1, COMPANY, text="Когда внесёте оплату?"),
                message(2, text="ПОЗВОНИТЕ МНЕ, ПОЖАЛУЙСТА")]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: REQUEST})
    assert 2 in result["response_required_ids"]


async def test_integrator_prefix_alone_never_opens_a_work():
    """Префикс интегратора в маркеры не считается: смотрим на слово клиента."""
    messages = [message(1, COMPANY, text="Когда внесёте оплату?"),
                message(2, text="(К) Людмила [corp.example.​com] пишет:   уточняю")]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: REQUEST})
    assert 2 not in result["response_required_ids"]


# ── Что окно НЕ гасит ────────────────────────────────────────────────────

async def test_mixed_request_inside_the_window_still_opens_its_own_work():
    """`mixed_request`: ответ «Давайте в Сбер» + поручение «в точку надо аренду»."""
    messages = [message(1580, COMPANY, seconds=0, text="В какой банк отправлять зарплату?"),
                message(1592, seconds=60, text="Давайте в Сбер. В точку надо аренду арендодателю")]
    items, result = await replay(messages, {1580: (None, False, "question", None), 1592: MIXED})
    assert 1592 in result["response_required_ids"]
    assert items[1592].opened_at == messages[1].sent_at


async def test_request_a_day_after_the_question_is_outside_the_window():
    """Окно живёт до того же времени следующего рабочего дня — не дольше.

    Давний вопрос не должен глотать заведомо новую просьбу.
    """
    messages = [message(1, COMPANY, seconds=0, text="Когда вам удобно созвониться?"),
                message(2, seconds=90000, text="Выставьте, пожалуйста, счет на оплату")]
    items, result = await replay(messages, {1: (None, False, "question", None), 2: REQUEST},
                                 after=timedelta(seconds=93600))
    assert 2 in result["response_required_ids"]
    assert items[2].opened_at == messages[1].sent_at


async def test_request_after_a_handoff_opens_its_own_work():
    """Окно взводит вопрос компании, а не передача специалисту.

    Просьба через полтора часа после «Передала бухгалтеру» получает свой
    срок даже с обычной меткой `request`: метки `handoff` окно не взводят.
    """
    messages = [message(2354, seconds=0, text="Нужна копия декларации 2025"),
                message(2360, COMPANY, seconds=60, text="Передала бухгалтеру"),
                message(3302, seconds=6000, text="Сформируйте, пожалуйста, три счёта")]
    items, result = await replay(messages, {2354: REQUEST, 2360: (None, False, HANDOFF, 2354),
                                            3302: REQUEST}, after=timedelta(seconds=9600))
    assert 3302 in result["response_required_ids"]
    assert items[3302].opened_at == messages[2].sent_at


async def test_request_after_a_statement_that_is_not_a_question_keeps_its_deadline():
    """Реплика компании без вопроса ход клиенту не передаёт — окна нет."""
    messages = [message(1, COMPANY, seconds=0, text="Платёжка ушла в банк."),
                message(2, seconds=60, text="Выставьте, пожалуйста, счет на оплату")]
    _, result = await replay(messages, {1: (None, True, "substantive", None), 2: REQUEST})
    assert 2 in result["response_required_ids"]


async def test_short_client_answer_does_not_use_up_the_window():
    """«Да.» ход не расходует — окно живёт до своего срока.

    Короткое подтверждение внутри окна остаётся ответом, и следующая
    реплика той же беседы — тоже ответ, пока не истёк срок окна и пока
    компания не сказала ничего содержательного.
    """
    messages = [message(2858, COMPANY, seconds=0, text="вы же напишете по итогу?"),
                message(2859, seconds=60, text="Да."),
                message(2860, seconds=360, text="Узнаю у какого банка проще .")]
    _, result = await replay(messages, {2858: (None, False, "question", None),
                                        2859: (False, None, "answer", None), 2860: QUESTION})
    assert 2859 not in result["response_required_ids"]
    assert 2860 not in result["response_required_ids"]


async def test_the_window_still_opens_on_a_question_mark_after_a_short_answer():
    """Окно пережило «Да.», но встречный вопрос его по-прежнему вскрывает."""
    messages = [message(2858, COMPANY, seconds=0, text="вы же напишете по итогу?"),
                message(2859, seconds=60, text="Да."),
                message(2860, seconds=360, text="А когда будет готово?")]
    items, result = await replay(messages, {2858: (None, False, "question", None),
                                            2859: (False, None, "answer", None), 2860: QUESTION})
    assert 2860 in result["response_required_ids"]
    assert items[2860].opened_at == messages[2].sent_at


async def test_a_call_request_after_a_short_answer_still_opens_a_work():
    """Та же связка с просьбой о звонке вместо знака вопроса."""
    messages = [message(2858, COMPANY, seconds=0, text="вы же напишете по итогу?"),
                message(2859, seconds=60, text="Да."),
                message(2860, seconds=360, text="Жду звонка по ЭТрН")]
    _, result = await replay(messages, {2858: (None, False, "question", None),
                                        2859: (False, None, "answer", None), 2860: REQUEST})
    assert 2860 in result["response_required_ids"]


async def test_the_window_survives_a_short_answer_but_not_its_deadline():
    """Срок окна прежний: то же время следующего рабочего дня, не дольше."""
    messages = [message(1, COMPANY, seconds=0, text="вы же напишете по итогу?"),
                message(2, seconds=60, text="Да."),
                message(3, seconds=90000, text="Выставьте, пожалуйста, счет на оплату")]
    items, result = await replay(messages, {1: (None, False, "question", None),
                                            2: (False, None, "answer", None), 3: REQUEST},
                                 after=timedelta(seconds=93600))
    assert 3 in result["response_required_ids"]
    assert items[3].opened_at == messages[2].sent_at


async def test_a_substantive_company_reply_closes_the_window():
    """Второй ограничитель окна: содержательная реплика компании без вопроса.

    После неё ход снова у компании, и просьба клиента получает свой срок
    даже внутри суток от прежнего вопроса.
    """
    messages = [message(1, COMPANY, seconds=0, text="вы же напишете по итогу?"),
                message(2, seconds=60, text="Да."),
                message(3, COMPANY, seconds=120, text="Платёжка ушла в банк."),
                message(4, seconds=180, text="Выставьте, пожалуйста, счет на оплату")]
    _, result = await replay(messages, {1: (None, False, "question", None),
                                        2: (False, None, "answer", None),
                                        3: (None, True, "substantive", None), 4: REQUEST},
                             after=timedelta(seconds=600))
    assert 4 in result["response_required_ids"]


@pytest.mark.parametrize("version", [2, 3])
async def test_v2_and_v3_spend_the_window_on_the_first_client_word(version):
    """Только v4: в v2/v3 окно гаснет первым словом клиента."""
    messages = [message(1, COMPANY, seconds=0, text="вы же напишете по итогу?"),
                message(2, seconds=60, text="Да."),
                message(3, seconds=360, text="Выставьте, пожалуйста, счет на оплату")]
    _, result = await replay(messages, {1: (None, False, "question", None),
                                        2: (False, None, "answer", None), 3: REQUEST},
                             version=version)
    assert 3 in result["response_required_ids"]


# ── Файл без подписи при закрытых работах ────────────────────────────────

async def test_bare_file_labelled_request_at_closed_works_still_needs_a_reaction():
    """Неучтённый документ без подписи при закрытых работах — не ответ; это правило сильнее окна."""
    messages = [message(1, COMPANY, seconds=0, text="Когда вам удобно созвониться?"),
                message(2, seconds=60, media="document")]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: REQUEST})
    assert 2 in result["response_required_ids"]


async def test_bare_file_without_a_verdict_inside_the_window_stays_an_answer():
    """Тот же файл без вердикта модели внутри окна — запрошенный документ, не обращение."""
    messages = [message(1, COMPANY, seconds=0, text="Пришлите, пожалуйста, договор"),
                message(2, seconds=60, media="document")]
    _, result = await replay(messages, {1: (None, False, "question", None)})
    assert 2 not in result["response_required_ids"]


# ── Границы версий ───────────────────────────────────────────────────────

@pytest.mark.parametrize("version", [2, 3])
async def test_v2_and_v3_are_untouched_by_the_answer_window_rules(version):
    """v2/v3 защищали ответ всегда — окно ответа их поведение не меняет."""
    messages = [message(1, COMPANY, text="Зарплату за август?"), message(2, text="Да, за август")]
    _, result = await replay(messages, {1: (None, False, "question", None), 2: REQUEST},
                             version=version)
    assert 2 not in result["response_required_ids"]
