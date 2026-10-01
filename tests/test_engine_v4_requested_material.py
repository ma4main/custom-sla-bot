"""Движок эпизодов: запрошенный материал, зачёт ответа соседу по пачке при
единственном ожидании специалиста и передача со встречным вопросом клиенту.

Метки, ссылки и авторство в тестах — заданный вход, а не предсказание модели.
"""

from datetime import timedelta

import pytest

from app.db.models import InteractionState
from app.services import episodes
from tests.test_classify_context_v11 import COMPANY, REQUEST, message
from tests.test_engine_v4_rules import (
    CLIENT_A, CLIENT_ACK, CLIENT_QUESTION, COMPANY_ACK, COMPANY_QUESTION,
    COMPANY_SUBSTANTIVE, KSENIA, IRINA, STAFF_KSENIA, STAFF_IRINA, STAFF_DARYA,
    _LATE_CORRECTION_VERDICTS, _SIBLING_QUESTION_STAFF, _SIBLING_QUESTION_VERDICTS,
    _WITHDRAWN_REQUEST_VERDICTS, _case_late_correction, _case_sibling_question,
    _case_withdrawn_request, members, msk, replay,
)

CLIENT_ANSWER = (False, None, "answer", None)
CLIENT_INFO = (False, None, "info", None)


# ── Запрошенный материал ─────────────────────────────────────────────────
#
# Файлы клиента после просьбы компании прислать материал — ответ, а не
# новое обращение, даже если в просьбе нет знака вопроса.

ASK_MATERIAL = "Пришлите, пожалуйста, акты за июнь."


def _case_material_request(ask=ASK_MATERIAL, *, with_ack=True):
    """Просьба компании без «?», затем два файла клиента без текста."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ask),
        message(2, seconds=msk(10, 30), author=CLIENT_A, media="document"),
        message(3, seconds=msk(10, 31), author=CLIENT_A, media="document"),
    ]
    verdicts = {1: COMPANY_SUBSTANTIVE}
    if with_ack:
        messages.append(message(4, COMPANY, seconds=msk(11, 15),
                                text=IRINA + "Спасибо, получили"))
        verdicts[4] = COMPANY_ACK
    return messages, verdicts


async def test_requested_files_are_an_answer_without_a_deadline():
    """Обращения нет, срока нет."""
    messages, verdicts = _case_material_request(with_ack=False)
    items, result = await replay(messages, verdicts, staff={1: STAFF_IRINA},
                                 after=timedelta(seconds=msk(11, 5)),
                                 settle_open=True)
    assert result["answers"] == [2, 3]
    assert result["response_required_ids"] == []
    assert items[2].state is InteractionState.NO_RESPONSE_NEEDED
    assert items[2].sla_breached is not True


async def test_a_late_thanks_for_the_requested_files_is_not_a_breach():
    """«Спасибо, получили» через 45 минут после файлов просрочкой не считается:
    на запрошенное реакция не требовалась."""
    messages, verdicts = _case_material_request()
    items, _ = await replay(messages, verdicts,
                            staff={1: STAFF_IRINA, 4: STAFF_IRINA},
                            settle_open=True)
    assert items[2].state is InteractionState.NO_RESPONSE_NEEDED
    assert items[2].sla_breached is not True


async def test_the_same_request_with_a_question_mark_is_an_answer_too():
    """Со знаком «?» файлы засчитываются ответом через окно ожидания ответа."""
    messages, verdicts = _case_material_request("Пришлите, пожалуйста, акты за июль?")
    _, result = await replay(messages, verdicts,
                             staff={1: STAFF_IRINA, 4: STAFF_IRINA},
                             settle_open=True)
    assert result["answers"] == [2, 3]


@pytest.mark.parametrize("text", [
    "И ещё нужна справка о штате",      # просьба без «?»
    "А справку о численности сделаете?",      # просьба со знаком вопроса
])
async def test_a_text_request_inside_the_material_state_still_opens_a_work(text):
    """Состояние глушит МАТЕРИАЛ, а не текст.

    Текстовая просьба судится как обычно и получает свой срок — просьба
    внутри ожидания материала не теряется.
    """
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, seconds=msk(10, 30), author=CLIENT_A, text=text),
    ]
    items, result = await replay(messages, {1: COMPANY_SUBSTANTIVE, 2: REQUEST},
                                 staff={1: STAFF_IRINA})
    assert 2 in result["response_required_ids"]
    assert items[2].state is InteractionState.OPEN


async def test_a_text_request_after_the_files_still_gets_its_own_deadline():
    """Файлы молчат, просьба следом — нет: два разных исхода в одной пачке."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, seconds=msk(10, 30), author=CLIENT_A, media="document"),
        message(3, seconds=msk(10, 35), author=CLIENT_A,
                text="И ещё нужна справка о штате"),
    ]
    items, result = await replay(messages, {1: COMPANY_SUBSTANTIVE, 3: REQUEST},
                                 staff={1: STAFF_IRINA}, settle_open=True)
    assert result["answers"] == [2]
    assert items[2].state is InteractionState.NO_RESPONSE_NEEDED
    assert items[3].state is InteractionState.OPEN
    assert 3 in result["response_required_ids"]


async def test_an_unrequested_file_still_opens_a_work():
    """Без предшествующей просьбы состояния нет."""
    messages = [message(1, seconds=msk(10, 30), author=CLIENT_A, media="document")]
    items, result = await replay(messages, {}, settle_open=True)
    assert result["answers"] == []
    assert items[1].state is InteractionState.OPEN


# ── Когда ожидание материала гаснет ──────────────────────────────────────
#
# Реплики самой компании до ответа клиента состояние не гасят: менеджер может
# прислать свои файлы и дописать к просьбе, а клиент пришлёт запрошенное позже.


def _case_company_addendum_before_the_answer(*, client_media=True, client_text=None,
                 client_seconds=msk(10, 35)):
    """Чат, где дописка компании к просьбе предшествует ответу клиента.

    Тексты сокращены до того, что решает: просьба о материале,
    СОБСТВЕННЫЕ файлы менеджера, её же дописка к просьбе — и только потом
    реплика клиента.
    """
    messages = [
        message(1, COMPANY, seconds=msk(10, 26),
                text=IRINA + "Пришлите, пожалуйста, по ооо альфа счет "
                              "118 и 119, а также акты к ним"),
        message(2, COMPANY, seconds=msk(10, 26, 40), media="document",
                text=IRINA + "делится файлом"),
        message(3, COMPANY, seconds=msk(10, 26, 50), media="document",
                text=IRINA + "делится файлом"),
        message(4, COMPANY, seconds=msk(10, 27, 18),
                text=IRINA + "по остальным отгрузкам закрывающие "
                              "документы во вложении, выше"),
        message(5, seconds=client_seconds, author=CLIENT_A,
                text=client_text,
                media="document" if client_media else None),
    ]
    verdicts = {1: COMPANY_SUBSTANTIVE, 4: COMPANY_SUBSTANTIVE}
    if client_text is not None:
        verdicts[5] = REQUEST
    staff = {1: STAFF_IRINA, 2: STAFF_IRINA, 3: STAFF_IRINA,
             4: STAFF_IRINA}
    return messages, verdicts, staff


async def test_a_company_addendum_before_the_client_answered_keeps_the_state():
    """Пока клиент молчит, реплики компании ожидание материала не отменяют.

    Ни собственные файлы менеджера, ни её дописка к своей же просьбе
    состояние не гасят.
    """
    messages, verdicts, staff = _case_company_addendum_before_the_answer()
    items, result = await replay(messages, verdicts, staff=staff,
                                 settle_open=True)
    assert result["answers"] == [5]
    assert items[5].state is InteractionState.NO_RESPONSE_NEEDED
    assert items[5].sla_breached is not True


async def test_an_unrelated_company_reply_before_the_client_answered_keeps_it():
    """Та же ветка, но реплика компании НЕ про эту просьбу.

    Отличить «дописка к просьбе» от «реплика на другую тему» движку
    нечем — и не нужно: пока клиент ничего не прислал, запрошенное всё
    ещё ждут. Цена — узкая: непрошеный файл в этом промежутке будет
    принят за запрошенный.
    """
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, COMPANY, seconds=msk(10, 10),
                text=IRINA + "Отчёт сдали, всё в порядке."),
        message(3, seconds=msk(10, 30), author=CLIENT_A, media="document"),
    ]
    items, result = await replay(
        messages, {1: COMPANY_SUBSTANTIVE, 2: COMPANY_SUBSTANTIVE},
        staff={1: STAFF_IRINA, 2: STAFF_IRINA}, settle_open=True)
    assert result["answers"] == [3]
    assert items[3].state is InteractionState.NO_RESPONSE_NEEDED


async def test_a_text_request_after_a_company_addendum_still_opens_a_work():
    """Текстовая просьба клиента срок получает всегда."""
    messages, verdicts, staff = _case_company_addendum_before_the_answer(
        client_media=False, client_text="И ещё нужна справка о штате")
    items, result = await replay(messages, verdicts, staff=staff)
    assert 5 in result["response_required_ids"]
    assert items[5].state is InteractionState.OPEN


async def test_a_file_two_working_days_after_the_addendum_opens_a_work():
    """Дописка срок состояния не продлевает: через два рабочих дня оно мертво."""
    messages, verdicts, staff = _case_company_addendum_before_the_answer(
        client_seconds=msk(11, 0, day=2))
    items, result = await replay(messages, verdicts, staff=staff,
                                 after=timedelta(days=2, hours=2))
    assert result["answers"] == []
    assert items[5].state is InteractionState.OPEN


async def test_a_company_reply_after_the_client_answered_clears_the_state():
    """«Спасибо, получили» состояние гасит — клиент уже прислал.

    Следующий НЕПРОШЕНЫЙ файл через час открывает обращение: серия ответа
    (десять минут) к тому времени мертва, состояние снято.
    """
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, seconds=msk(10, 30), author=CLIENT_A, media="document"),
        message(3, COMPANY, seconds=msk(10, 35), text=IRINA + "Спасибо, получили"),
        message(4, seconds=msk(11, 40), author=CLIENT_A, media="document"),
    ]
    items, result = await replay(messages, {1: COMPANY_SUBSTANTIVE, 3: COMPANY_ACK},
                                 staff={1: STAFF_IRINA, 3: STAFF_IRINA},
                                 settle_open=True)
    assert result["answers"] == [2]
    assert items[4].state is InteractionState.OPEN


async def test_a_new_material_request_resets_the_moment():
    """Новая просьба не гасит, а ПЕРЕУСТАНАВЛИВАЕТ момент и счёт срока.

    Первая просьба в понедельник, вторая — во вторник утром: файл во
    вторник днём укладывается в срок ВТОРОЙ, хотя срок первой давно истёк.
    """
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, COMPANY, seconds=msk(10, 0, day=1),
                text=IRINA + "Пришлите, пожалуйста, ещё и акты за август"),
        message(3, seconds=msk(15, 0, day=1), author=CLIENT_A, media="document"),
    ]
    _, result = await replay(messages, {1: COMPANY_SUBSTANTIVE,
                                        2: COMPANY_SUBSTANTIVE},
                             staff={1: STAFF_IRINA, 2: STAFF_IRINA},
                             after=timedelta(days=1, hours=8), settle_open=True)
    assert result["answers"] == [3]


async def test_a_new_material_request_reopens_the_conversation():
    """Переустановка сбрасывает и признак «клиент уже начал отвечать».

    Иначе дописка компании к ВТОРОЙ просьбе гасила бы состояние, потому
    что по первой клиент уже присылал.
    """
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, seconds=msk(10, 30), author=CLIENT_A, media="document"),
        message(3, COMPANY, seconds=msk(11, 0),
                text=IRINA + "Спасибо. Пришлите, пожалуйста, ещё акты за август"),
        message(4, COMPANY, seconds=msk(11, 1),
                text=IRINA + "по остальным реализациям документы выше"),
        message(5, seconds=msk(11, 30), author=CLIENT_A, media="document"),
    ]
    items, result = await replay(
        messages, {1: COMPANY_SUBSTANTIVE, 3: COMPANY_SUBSTANTIVE,
                   4: COMPANY_SUBSTANTIVE},
        staff={1: STAFF_IRINA, 3: STAFF_IRINA, 4: STAFF_IRINA},
        settle_open=True)
    assert result["answers"] == [2, 5]
    assert all(item.state is InteractionState.NO_RESPONSE_NEEDED
               for item in items.values())


async def test_the_material_state_lives_until_the_next_working_morning():
    """Срок тот же, что у окна ответа: то же время следующего рабочего дня."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, seconds=msk(9, 30, day=1), author=CLIENT_A, media="document"),
    ]
    _, result = await replay(messages, {1: COMPANY_SUBSTANTIVE},
                             staff={1: STAFF_IRINA},
                             after=timedelta(days=1, hours=4), settle_open=True)
    assert result["answers"] == [2]


async def test_the_material_state_expires_like_the_answer_window():
    """Файл через два рабочих дня — обычное вложение без подписи."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, seconds=msk(11, 0, day=2), author=CLIENT_A, media="document"),
    ]
    items, result = await replay(messages, {1: COMPANY_SUBSTANTIVE},
                                 staff={1: STAFF_IRINA},
                                 after=timedelta(days=2, hours=2))
    assert result["answers"] == []
    assert items[2].state is InteractionState.OPEN


async def test_a_caption_with_an_answer_label_joins_the_same_batch():
    """«Отправила, вот акты» с меткой `answer` — тоже ответ, и серия жива."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, seconds=msk(10, 30), author=CLIENT_A, text="Отправила, вот акты"),
        message(3, seconds=msk(10, 31), author=CLIENT_A, media="document"),
    ]
    _, result = await replay(messages, {1: COMPANY_SUBSTANTIVE, 2: CLIENT_ANSWER},
                             staff={1: STAFF_IRINA}, settle_open=True)
    assert result["answers"] == [2, 3]
    assert members(result)[2] == [2, 3]


@pytest.mark.parametrize("verdict", [CLIENT_ACK, CLIENT_INFO])
async def test_a_short_acknowledgement_inside_the_state_is_an_answer(verdict):
    """Метки `ack`/`info` внутри состояния идут тем же путём, что вложение."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + ASK_MATERIAL),
        message(2, seconds=msk(10, 30), author=CLIENT_A, text="Хорошо, сделаю"),
    ]
    _, result = await replay(messages, {1: COMPANY_SUBSTANTIVE, 2: verdict},
                             staff={1: STAFF_IRINA}, settle_open=True)
    assert result["answers"] == [2]


async def test_a_request_right_after_an_attachment_in_the_answer_window_is_an_answer():
    """Окно ожидания ответа взведено знаком вопроса, и просьба без «?» сразу
    после вложения засчитывается ответом; ожидание материала здесь ни при чём."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0),
                text=IRINA + "Акты за июль пришлёте сегодня?"),
        message(2, seconds=msk(10, 30), author=CLIENT_A, media="document"),
        message(3, seconds=msk(10, 31), author=CLIENT_A,
                text="И ещё нужна справка о штате"),
    ]
    _, result = await replay(messages, {1: COMPANY_QUESTION, 3: REQUEST},
                             staff={1: STAFF_IRINA}, settle_open=True)
    assert result["answers"] == [2, 3]


# ── Формы просьбы прислать материал ──────────────────────────────────────
#
# Цена ошибки здесь ПРОПУСК, а не ложный алерт: лишний раз взведённое
# состояние глушит вложение клиента на сутки. Поэтому список короткий,
# и границы у него проверяются отдельно от движка.

MATERIAL_REQUESTS = [
    "Пришлите, пожалуйста, акты за июнь.",
    "Перешлите ответ банка.",
    "Вышлите скан доверенности.",
    "Отправьте, пожалуйста, УПД за сентябрь.",
    "Направьте подписанный договор.",
    "Предоставьте выписку по 51 счёту.",
    "Приложите скан платёжки.",
    "Прикрепите фото чека.",
    "Загрузите акты в облако, пожалуйста.",
    "Скиньте, пожалуйста, реквизиты одним файлом.",
    "Сбросьте фото подписанного акта.",
    "Сфотографируйте первую страницу.",
    "Отсканируйте оба экземпляра.",
    "Прошу предоставить акты за июль.",
    "Нужно прислать акты за июль.",
    "Просьба направить скан доверенности.",
    "Необходимо загрузить документы до конца дня.",
    # «в ЛК» здесь предмет просьбы, а не канал: отсекать по предлогу нельзя,
    # иначе настоящая просьба о материале потеряется.
    "Пожалуйста, пришлите код для входа в кабинет",
    # Адресат назван прямо — это просьба к клиенту, а не инструкция
    # про третью сторону.
    "Пока запись остаётся, вам нужно отправить нам в чат документы, подтверждающие адрес",
    # Почта в СОСЕДНЕЙ фразе просьбу не отменяет.
    "На почту отправила список. Просьба выслать закрывающие документы, можно частично",
    "62.01 Разработка компьютерного программного обеспечения. Пришлите, пожалуйста, устав",
]

NOT_MATERIAL_REQUESTS = [
    # Изъявительное наклонение: рассказ о сделанном.
    "Документы пришли, спасибо.",
    "Мы вам всё отправили ещё вчера.",
    "Акты направили на почту.",
    "Выслали вчера вечером.",
    # Формы единственного числа: компания пишет клиенту на «вы», а
    # «пришли» вдобавок омоним прошедшего времени.
    "Деньги пришли на счёт.",
    # Просьба о ДЕЙСТВИИ, а не о материале.
    "Подпишите договор у директора.",
    "Заполните форму в личном кабинете.",
    "Согласуйте сумму с руководителем.",
    "Оплатите счёт до пятницы.",
    # Не про материал вовсе.
    "Сообщите, когда будет удобно.",
    "Дайте знать о решении.",
    "Покажите пример прошлого года.",
    # Голый инфинитив — жалоба, а не поручение.
    "Прислать акты в этом месяце не получится.",
    "Отправить сегодня не выйдет, сервис лежит.",
    # Другой канал: вложение в чате такую просьбу не закрывает.
    "Продублируйте на почту, пожалуйста.",
    # Форма просьбы настоящая, но материал просят не в чат —
    # состояние «ждём материал» взводить нечем.
    "Просьба выслать скан\\фото нам на почту",
    "Пришлите, пожалуйста, выписку на почту buh@example.com",
    "Отправьте акты по электронной почте.",
    # «Нужно/необходимо … отправить/направить» — инструкция про третью сторону.
    "Его нужно подать через оперативную помощь service.nalog.ru/ens-help/ Вот ссылка.",
    "Для смены тарифа необходимо отправить заявление в Сбис",
    "Надо направить требование в банк",
]


@pytest.mark.parametrize("text", MATERIAL_REQUESTS)
def test_every_material_request_is_recognised(text):
    assert episodes.material_request(text) is True


@pytest.mark.parametrize("text", NOT_MATERIAL_REQUESTS)
def test_the_marker_is_silent_on_everything_else(text):
    assert episodes.material_request(text) is False


@pytest.mark.parametrize("text", NOT_MATERIAL_REQUESTS)
async def test_a_reply_outside_the_list_arms_no_state(text):
    """Сквозная проверка того же: вложение после такой реплики — обращение."""
    messages = [
        message(1, COMPANY, seconds=msk(10, 0), text=IRINA + text),
        message(2, seconds=msk(10, 30), author=CLIENT_A, media="document"),
    ]
    items, result = await replay(messages, {1: COMPANY_SUBSTANTIVE},
                                 staff={1: STAFF_IRINA}, settle_open=True)
    assert result["answers"] == []
    assert items[2].state is InteractionState.OPEN


# ── Второй слой при ЕДИНСТВЕННОМ ожидании ────────────────────────────────
#
# Результат специалиста со ссылкой на соседа по пачке закрывает единственное
# ожидание — и когда сосед открыт, и когда он уже закрыт.


def _case_result_linked_to_an_open_sibling(result_verdict=(None, True, "substantive", 2)):
    """Результат специалиста со ссылкой на открытого соседа по пачке."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Банк отклоняет платёжку по новому контрагенту"),
        message(2, seconds=msk(10, 1), author=CLIENT_A,
                text="Что с лимитом по счёту?"),
        message(3, COMPANY, seconds=msk(10, 5),
                text=IRINA + "Передала бухгалтеру вопрос по банку"),
        message(4, COMPANY, seconds=msk(11, 30),
                text=KSENIA + "Банк подтвердил, ограничение снято, платёжка пройдёт"),
    ]
    verdicts = {1: REQUEST, 2: CLIENT_QUESTION,
                3: (None, False, "handoff", 1), 4: result_verdict}
    return messages, verdicts, {3: STAFF_IRINA, 4: STAFF_KSENIA}


def _case_result_linked_to_a_closed_sibling():
    """Ссылка на уже ЗАКРЫТОГО соседа той же пачки."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Нужен акт сверки с Вектором"),
        message(2, seconds=msk(10, 1), author=CLIENT_A,
                text="И когда будет расчёт по отпускным?"),
        message(3, COMPANY, seconds=msk(10, 10),
                text=IRINA + "Передала бухгалтеру по акту"),
        message(4, COMPANY, seconds=msk(10, 20),
                text=IRINA + "Отпускные будут рассчитаны к четвергу"),
        message(5, COMPANY, seconds=msk(12, 0),
                text=KSENIA + "Акт сверки подписала, отправила на почту"),
    ]
    verdicts = {1: REQUEST, 2: CLIENT_QUESTION,
                3: (None, False, "handoff", 1),
                4: (None, True, "substantive", 2),
                5: (None, True, "substantive", 2)}
    return messages, verdicts, {3: STAFF_IRINA, 4: STAFF_IRINA, 5: STAFF_KSENIA}


async def test_a_result_linked_to_an_open_sibling_closes_the_only_wait():
    """Результат по существу закрывает ожидание так же, как верная ссылка."""
    messages, verdicts, staff = _case_result_linked_to_an_open_sibling()
    items, _ = await replay(messages, verdicts, staff=staff, after=timedelta(days=2))
    assert items[1].substantive_at == messages[3].sent_at
    assert items[1].substantive_breached is False
    assert items[1].state is InteractionState.ANSWERED


async def test_a_result_linked_to_a_closed_sibling_closes_the_only_wait():
    """Ссылка промахнулась в закрытого соседа — ожидание всё равно закрыто.

    Разбор по репликам:
      * #4 «Отпускные будут рассчитаны к четвергу» (ссылка на открытого
        соседа #2) ожидание НЕ закрывает: её написала Ирина — та же, что
        делала передачу. Помощник, ответивший по соседней теме пачки,
        выходом специалиста не является;
      * #5 «Акт сверки подписан» от Ксении ссылается на уже ЗАКРЫТОГО
        соседа #2, и ожидание закрывает именно она — в 12:00.
    """
    messages, verdicts, staff = _case_result_linked_to_a_closed_sibling()
    items, _ = await replay(messages, verdicts, staff=staff, after=timedelta(days=2))
    assert items[1].substantive_at == messages[4].sent_at
    assert items[1].substantive_staff_id == STAFF_KSENIA
    assert items[1].state is InteractionState.ANSWERED


async def test_the_missed_link_alone_is_enough_to_find_the_wait():
    """Единственный путь к ожиданию — промах ссылки.

    Передача ушла по ВТОРОЙ просьбе пачки, первая закрылась сама
    («помощник ответил сам»), и результат специалиста сослался на её
    закрытое сообщение.
    """
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Когда будет расчёт по отпускным?"),
        message(2, seconds=msk(10, 1), author=CLIENT_A,
                text="И нужен акт сверки с Вектором"),
        message(3, COMPANY, seconds=msk(10, 10),
                text=IRINA + "Передала бухгалтеру по акту"),
        message(4, COMPANY, seconds=msk(12, 0),
                text=KSENIA + "Акт сверки подписала, отправила на почту"),
    ]
    verdicts = {1: REQUEST, 2: CLIENT_QUESTION,
                3: (None, False, "handoff", 2),
                4: (None, True, "substantive", 1)}
    items, _ = await replay(messages, verdicts, staff={3: STAFF_IRINA, 4: STAFF_KSENIA},
                            after=timedelta(days=2))
    assert items[1].state is InteractionState.ANSWERED   # закрылась сама
    assert items[2].handoff_at == messages[2].sent_at
    assert items[2].substantive_at == messages[3].sent_at


async def test_the_missed_link_path_still_needs_the_same_batch():
    """Закрытая работа ДРУГОЙ пачки соседом не считается."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Когда будет расчёт по отпускным?"),
        message(2, COMPANY, seconds=msk(10, 2), text=IRINA + "Приняла в работу"),
        message(3, seconds=msk(10, 5), author=CLIENT_A, text="И нужен акт сверки"),
        message(4, COMPANY, seconds=msk(10, 10),
                text=IRINA + "Передала бухгалтеру по акту"),
        message(5, COMPANY, seconds=msk(12, 0),
                text=KSENIA + "Акт сверки подписала, отправила на почту"),
    ]
    verdicts = {1: REQUEST, 2: (None, False, "ack", None), 3: REQUEST,
                4: (None, False, "handoff", 3),
                5: (None, True, "substantive", 1)}
    items, _ = await replay(messages, verdicts,
                            staff={2: STAFF_IRINA, 4: STAFF_IRINA, 5: STAFF_KSENIA},
                            after=timedelta(days=2))
    assert items[1].first_reaction_message_id == 2
    assert items[3].first_reaction_message_id == 4
    assert items[3].substantive_at is None


async def test_a_correct_link_still_closes_its_own_wait():
    """Реплика со ссылкой на ждущую работу закрывает её всегда."""
    messages, verdicts, staff = _case_result_linked_to_a_closed_sibling()
    verdicts = {**verdicts, 5: (None, True, "substantive", 1)}
    items, _ = await replay(messages, verdicts, staff=staff, after=timedelta(days=2))
    assert items[1].substantive_at is not None
    assert items[1].state is InteractionState.ANSWERED


async def test_the_handing_manager_never_closes_the_wait_by_answering_a_sibling():
    """Ответ передавшего менеджера по соседней теме ожидание не закрывает.

    Тот же вход, но ссылка #5 ВЕРНА (указывает на ждущую работу #1), и
    закрыть ожидание должна именно она — в 12:00. Реплика #4 про зарплату
    в 10:20 написана передавшей Ириной, поэтому соседу не засчитывается.
    """
    messages, verdicts, staff = _case_result_linked_to_a_closed_sibling()
    verdicts = {**verdicts, 5: (None, True, "substantive", 1)}
    items, _ = await replay(messages, verdicts, staff=staff, after=timedelta(days=2))
    assert items[1].substantive_at == messages[4].sent_at
    assert items[1].substantive_staff_id == STAFF_KSENIA


async def test_the_sibling_result_needs_another_staff_member_behind_it():
    """Обе стороны условия автора на одном входе.

    Ссылка #4 промахнулась в соседа #2, ожидание в чате одно. Кто написал
    #4 — и решает: вышедший специалист Ксения закрывает, передавшая Ирина
    своим же ответом по соседней теме — нет.
    """
    messages, verdicts, _ = _case_result_linked_to_an_open_sibling()
    other, _ = await replay(messages, verdicts,
                            staff={3: STAFF_IRINA, 4: STAFF_KSENIA},
                            after=timedelta(days=2))
    assert other[1].substantive_at == messages[3].sent_at
    same, _ = await replay(messages, verdicts,
                           staff={3: STAFF_IRINA, 4: STAFF_IRINA},
                           after=timedelta(days=2))
    assert same[1].substantive_at is None


@pytest.mark.parametrize("staff", [
    {3: STAFF_IRINA},              # автор ответа неизвестен
    {4: STAFF_KSENIA},               # неизвестен автор передачи
    {},                             # оба неизвестны
])
async def test_the_sibling_result_needs_both_authors_attributed(staff):
    """Неизвестный автор доказательством не служит.

    Соседу ожидание достаётся по догадке о пачке; класть сверху вторую
    догадку («может быть, это другой сотрудник») нельзя. Иначе, чем
    для работы, в которую ссылка попала точно: там неатрибутированный
    автор после передачи засчитывается выходом специалиста.
    """
    messages, verdicts, _ = _case_result_linked_to_an_open_sibling()
    items, _ = await replay(messages, verdicts, staff=staff,
                            after=timedelta(days=2))
    assert items[1].substantive_at is None


async def test_a_promise_by_another_employee_does_not_close_the_sibling_wait():
    """Соседу засчитываются только результат и встречный вопрос.

    Реплика другого сотрудника без результата (`promise`) соседу ожидание
    НЕ закрывает: выход на связь без ссылки на ждущую работу — не зачёт.
    """
    messages, verdicts, staff = _case_result_linked_to_an_open_sibling(
        (None, False, "promise", 2))
    items, _ = await replay(messages, verdicts, staff=staff,
                            after=timedelta(days=2))
    assert items[1].substantive_at is None


async def test_a_handoff_is_never_handed_to_the_batch():
    """Передача соседу не раздаётся: «передала бухгалтеру» по одной просьбе
    не гасит срок по другой."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A, text="Не проходит платёж"),
        message(2, seconds=msk(10, 1), author=CLIENT_A, text="И что с актом сверки?"),
        message(3, COMPANY, seconds=msk(10, 5),
                text=IRINA + "Передала бухгалтеру про платёж"),
        message(4, COMPANY, seconds=msk(10, 30),
                text=KSENIA + "И по акту тоже передала"),
    ]
    verdicts = {1: REQUEST, 2: CLIENT_QUESTION,
                3: (None, False, "handoff", 1), 4: (None, False, "handoff", 2)}
    items, _ = await replay(messages, verdicts,
                            staff={3: STAFF_IRINA, 4: STAFF_KSENIA},
                            after=timedelta(days=2))
    assert items[1].substantive_at is None


async def test_a_sibling_from_another_batch_is_not_a_sibling():
    """Соседство доказывается ОБЩЕЙ первой реакцией, а не соседством во времени."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A, text="Не проходит платёж"),
        message(2, COMPANY, seconds=msk(10, 2), text=IRINA + "Приняла в работу"),
        message(3, seconds=msk(10, 5), author=CLIENT_A, text="И что с актом сверки?"),
        message(4, COMPANY, seconds=msk(10, 10),
                text=IRINA + "Передала бухгалтеру про платёж"),
        message(5, COMPANY, seconds=msk(11, 30),
                text=KSENIA + "Акт сверки подписала, отправила на почту"),
    ]
    verdicts = {1: REQUEST, 2: (None, False, "ack", None), 3: REQUEST,
                4: (None, False, "handoff", 1), 5: (None, True, "substantive", 3)}
    items, _ = await replay(messages, verdicts,
                            staff={2: STAFF_IRINA, 4: STAFF_IRINA, 5: STAFF_KSENIA},
                            after=timedelta(days=2))
    assert items[1].first_reaction_message_id == 2
    assert items[3].first_reaction_message_id == 4
    assert items[1].substantive_at is None


# ── Два ожидания в одной пачке ───────────────────────────────────────────
#
# При двух и больше ожиданиях движок не угадывает, кому засчитать ответ:
# закрывается только работа, указанная ссылкой.


def _case_two_handoffs_of_one_batch():
    """Две передачи одной пачки, встречный вопрос по второй."""
    messages = [
        message(1, seconds=msk(10, 0), author=CLIENT_A,
                text="Не проходит платёж контрагенту"),
        message(2, seconds=msk(10, 1), author=CLIENT_A, text="И что с актом сверки?"),
        message(3, COMPANY, seconds=msk(10, 5),
                text=IRINA + "Передала бухгалтеру про платёж"),
        message(4, COMPANY, seconds=msk(10, 6),
                text=IRINA + "И по акту тоже передала"),
        message(5, COMPANY, seconds=msk(11, 30),
                text=KSENIA + "Подскажите, акт нужен с печатью?"),
    ]
    verdicts = {1: REQUEST, 2: CLIENT_QUESTION,
                3: (None, False, "handoff", 1), 4: (None, False, "handoff", 2),
                5: (None, False, "question", 2)}
    staff = {3: STAFF_IRINA, 4: STAFF_IRINA, 5: STAFF_KSENIA}
    return messages, verdicts, staff


async def test_two_waits_of_one_batch_are_not_guessed():
    """Встречный вопрос закрывает только указанное ожидание; второе висит."""
    messages, verdicts, staff = _case_two_handoffs_of_one_batch()
    items, _ = await replay(messages, verdicts, staff=staff, after=timedelta(days=2))
    assert items[2].substantive_at == messages[4].sent_at
    assert items[1].substantive_at is None
    assert items[1].state is InteractionState.REACTED


async def test_a_counter_question_linked_to_a_sibling_fulfils_the_only_wait():
    """Второй передачи нет — ожидание в чате одно, и встречный вопрос
    специалиста со ссылкой на соседа по пачке засчитывается ему."""
    messages, verdicts, staff = _case_two_handoffs_of_one_batch()
    messages = [msg for msg in messages if msg.id != 4]
    verdicts = {key: value for key, value in verdicts.items() if key != 4}
    items, _ = await replay(messages, verdicts, staff=staff, after=timedelta(days=2))
    assert items[1].substantive_at == messages[3].sent_at


# ── Передача со встречным вопросом клиенту ─────────────────────────────
#
# Реплика `handoff`, кончающаяся вопросом клиенту, ставит `substantive_at`
# тем же временем, что и `handoff_at`: второй слой закрывается в секунду
# открытия. Это принятое поведение, а не дефект: ждать реакции клиента
# на такую реплику не нужно.


def _case_handoff_with_a_counter_question(
    handoff_text="Передала ваш запрос Ксении, подскажите, удалось войти?",
):
    """Передача специалисту и вопрос клиенту в одной реплике."""
    messages = [
        message(1, seconds=msk(12, 0), author=CLIENT_A,
                text="Не могу войти в личный кабинет, посмотрите"),
        message(2, COMPANY, seconds=msk(12, 3), text=IRINA + handoff_text),
        message(3, seconds=msk(12, 10), author=CLIENT_A,
                text="И ещё нужен акт сверки за август"),
    ]
    verdicts = {1: REQUEST, 2: (None, False, "handoff", 1), 3: REQUEST}
    return messages, verdicts


def _case_handoff_with_a_counter_question_about_sessions():
    """Тот же рисунок с другим текстом: единственная просьба в чате."""
    messages = [
        message(863, seconds=0, author=CLIENT_A,
                text="Здравствуйте, закройте пожалуйста лишние сеансы"),
        message(870, COMPANY, seconds=304,
                text=IRINA + "Здравствуйте, передала ваш запрос, "
                              "подскажите удалось войти?"),
    ]
    verdicts = {863: REQUEST, 870: (None, False, "handoff", 863)}
    return messages, verdicts


async def test_a_handoff_that_asks_the_client_back_fulfils_its_own_wait():
    """Передача со встречным вопросом выполняет своё ожидание сразу.

    Проверяются оба входа — это одна и та же реплика: передача и встречный
    вопрос в одной фразе одного менеджера. Ожидание специалиста по ней
    не начинается (`substantive_at` = момент той же реплики), срок
    не нарушается, и кандидатом на алерт второго слоя обращение не становится.
    """
    for messages, verdicts, opener, staff in (
        (*_case_handoff_with_a_counter_question(), 1, {2: STAFF_IRINA}),
        (*_case_handoff_with_a_counter_question_about_sessions(), 863, {870: STAFF_IRINA}),
    ):
        items, _ = await replay(messages, verdicts, staff=staff,
                                after=timedelta(days=2))
        item = items[opener]
        assert item.handoff_at == messages[1].sent_at
        assert item.substantive_at == messages[1].sent_at
        assert item.substantive_message_id == messages[1].id
        assert item.substantive_breached is False
        # Кандидат второго слоя — это REACTED с передачей и БЕЗ ответа
        # (`alerts.py`, выборка `candidates`). Здесь ответ есть.
        assert not (item.state is InteractionState.REACTED
                    and item.substantive_at is None)


# ── Правила вместе ───────────────────────────────────────────────────────


async def test_the_material_state_and_the_sibling_credit_hold_together():
    """Зачёт соседу и ожидание материала друг другу не мешают."""
    # Результат специалиста со ссылкой на открытого соседа по пачке.
    messages, verdicts, staff = _case_result_linked_to_an_open_sibling()
    open_sibling_items, _ = await replay(messages, verdicts, staff=staff,
                                         after=timedelta(days=2))
    assert open_sibling_items[1].substantive_at is not None

    # Ссылка на уже закрытого соседа той же пачки.
    messages, verdicts, staff = _case_result_linked_to_a_closed_sibling()
    closed_sibling_items, _ = await replay(messages, verdicts, staff=staff,
                                           after=timedelta(days=2))
    assert closed_sibling_items[1].substantive_at is not None

    # Запрошенные файлы — ответ, срока нет.
    messages, verdicts = _case_material_request(with_ack=False)
    material_items, material_result = await replay(
        messages, verdicts, staff={1: STAFF_IRINA},
        after=timedelta(seconds=msk(11, 5)), settle_open=True)
    assert material_result["answers"] == [2, 3]
    assert material_items[2].state is InteractionState.NO_RESPONSE_NEEDED


async def test_the_late_correction_sibling_question_and_withdrawal_stay_fixed():
    """Поправка к молча закрывшейся своей работе, встречный вопрос со ссылкой
    на соседа и отзыв просьбы не дают ложных алертов."""
    sibling_items, _ = await replay(_case_sibling_question(), _SIBLING_QUESTION_VERDICTS,
                                    staff=_SIBLING_QUESTION_STAFF,
                                    after=timedelta(hours=20))
    assert sibling_items[6481].handoff_at is not None
    assert sibling_items[6481].substantive_at is not None

    correction_items, _ = await replay(_case_late_correction(), _LATE_CORRECTION_VERDICTS,
                                       staff={6972: STAFF_DARYA, 6976: STAFF_DARYA,
                                              6977: STAFF_DARYA, 6978: STAFF_DARYA,
                                              7030: STAFF_KSENIA})
    assert correction_items[6979].sla_breached is not True

    withdrawn_items, withdrawn_result = await replay(
        _case_withdrawn_request(), _WITHDRAWN_REQUEST_VERDICTS,
        staff={6716: STAFF_IRINA}, after=timedelta(days=2))
    assert withdrawn_items[6711].state is InteractionState.NO_RESPONSE_NEEDED
    assert withdrawn_result["interactions"] == 1
    assert members(withdrawn_result)[6711] == [6711, 6758]


async def test_an_answer_with_a_request_and_an_unlabelled_message_keep_their_outcome():
    """Случаи вне ожидания материала и зачёта соседу."""
    # «И ещё нужна справка» внутри окна ответа — ответ.
    answer_with_request_messages = [
        message(1, seconds=msk(9, 0), author=CLIENT_A, text="Нужен акт сверки"),
        message(2, COMPANY, seconds=msk(9, 10), text=IRINA + "Передала бухгалтеру"),
        message(3, COMPANY, seconds=msk(10, 0),
                text=KSENIA + "Уточните, за июль или за третий квартал?"),
        message(4, seconds=msk(10, 5), author=CLIENT_A,
                text="За июль. и ещё нужна справка о штате"),
    ]
    _, result = await replay(answer_with_request_messages,
                             {1: REQUEST, 2: (None, False, "handoff", 1),
                              3: (None, False, "question", 1), 4: REQUEST},
                             staff={2: STAFF_IRINA, 3: STAFF_KSENIA})
    assert 4 in result["answers"]

    # Реплика клиента БЕЗ вердикта открывает обращение.
    blank = [message(1, seconds=msk(10, 0), author=CLIENT_A, text="Что со сверкой")]
    items, result = await replay(blank, {})
    assert items[1].state is InteractionState.OPEN
    assert 1 in result["response_required_ids"]
