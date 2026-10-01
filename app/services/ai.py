"""Клиент ИИ-классификатора: OpenAI-совместимый API (`AI_BASE_URL`).

Важно: клиент не трогает базу во время HTTP-вызова: бюджет считается один раз
до прохода, расход копится в памяти и пишется короткой транзакцией после
(`month_tokens` / `record_usage`). Месячный потолок токенов — жёсткий предохранитель.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic, perf_counter
from zoneinfo import ZoneInfo

import aiohttp
import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AiUsage

log = structlog.get_logger(__name__)

# Метка формата входа классификатора (хвост, открытые обращения, маркер
# вложения у текущего сообщения), а не редакция текста промпта; по ней тесты
# сверяют отпечатки промпта. В базу пишется `PromptProfile.db_version` профиля.
PROMPT_VERSION = 14

# Потолок ответа модели по умолчанию (`CallParams.max_tokens`); резерв при
# проверке бюджета берётся из `max_tokens` профиля вызова.
MAX_COMPLETION_TOKENS = 500

# Пояс входа прибит к МСК: вход должен быть воспроизводим побайтно
# (тесты сравнивают строки), а пояс графика владелец может сменить.
PROMPT_TZ = ZoneInfo("Europe/Moscow")

TAIL_EVENTS = 6
TAIL_TEXT_LIMIT = 300

OPEN_ITEMS = 3
OPEN_ITEM_TEXT_LIMIT = 200

# Вложение подаётся маркером: документ, фото и голосовое различаются,
# остальное — общий `[файл]`. Содержимое вложений не распознаётся.
_MEDIA_MARKS = {
    "document": "[документ]",
    "photo": "[фото]",
    "voice": "[голосовое]",
}
_MEDIA_MARK_DEFAULT = "[файл]"


@dataclass(frozen=True)
class TailEvent:
    """Одно событие переписки для хвоста промпта.

    Воркер и тесты собирают одну и ту же структуру и обязаны давать одинаковую строку.
    """

    sent_at: datetime
    is_company: bool
    text: str | None = None
    media_kind: str | None = None


@dataclass(frozen=True)
class OpenItem:
    """Открытое обращение чата на момент классификации сообщения.

    Состояние выводится из полей, а не из `state`, чтобы одинаково работать
    на сохранённых обращениях и на пересчитанных в памяти.
    """

    message_id: int
    opened_at: datetime
    text: str | None = None
    first_reaction_at: datetime | None = None
    handoff_at: datetime | None = None
    substantive_at: datetime | None = None
    media_kind: str | None = None
    has_media: bool = False

    @property
    def state_label(self) -> str:
        """Состояние словами — ровно три варианта, как в контракте входа."""
        if self.first_reaction_at is None:
            return "ждём первой реакции"
        if self.handoff_at is not None and self.substantive_at is None:
            return "передано специалисту"
        return "в работе"


def _one_line(text: str | None, limit: int) -> str:
    """Строка без переносов и лишних пробельных символов, обрезанная по лимиту
    (в хвосте одно событие — одна строка)."""
    return " ".join((text or "").split())[:limit]


def _tail_line(event: TailEvent) -> str:
    who = "компания" if event.is_company else "клиент"
    body = _one_line(event.text, TAIL_TEXT_LIMIT)
    if event.media_kind or not body:
        # Подпись к вложению идёт после маркера: «[документ]: счёт за август».
        mark = _MEDIA_MARKS.get(event.media_kind or "", _MEDIA_MARK_DEFAULT)
        body = f"{mark}: {body}" if body else mark
    # Дата обязательна: хвост растягивается на несколько суток.
    return f"[{event.sent_at.astimezone(PROMPT_TZ):%d.%m %H:%M}] {who}: {body}"


def _tail_block(tail: Iterable[TailEvent] | None) -> str:
    """Хвост в хронологическом порядке; последние TAIL_EVENTS событий."""
    lines = [_tail_line(event) for event in list(tail or ())[-TAIL_EVENTS:]]
    return "\n".join(lines) if lines else "(нет)"


def _open_items_block(open_items: Iterable[OpenItem] | None) -> str:
    lines = [
        f"#{item.message_id} [{item.opened_at.astimezone(PROMPT_TZ):%d.%m %H:%M}] "
        f"{item.state_label}: {_one_line(current_message_text(item.text, has_media=item.has_media, media_kind=item.media_kind), OPEN_ITEM_TEXT_LIMIT)}"
        for item in list(open_items or ())[-OPEN_ITEMS:]
    ]
    return "\n".join(lines) if lines else "(нет)"


def client_payload(
    text: str,
    tail: Iterable[TailEvent] | None = (),
    open_items: Iterable[OpenItem] | None = (),
) -> str:
    """Вход для сообщения клиента: хвост, открытые обращения, сообщение.

    Будущие сообщения не подаются никогда: иначе вердикт зависел бы от момента
    классификации и переспрос менял бы разметку прошлого.
    """
    return (
        "=== ПРЕДЫДУЩИЕ СООБЩЕНИЯ ===\n"
        f"{_tail_block(tail)}\n"
        "=== ОТКРЫТЫЕ ОБРАЩЕНИЯ ===\n"
        f"{_open_items_block(open_items)}\n"
        "=== СООБЩЕНИЕ КЛИЕНТА ===\n"
        f"{text}\n==="
    )


def current_message_text(
    text: str | None,
    *,
    has_media: bool = False,
    media_kind: str | None = None,
) -> str:
    """Текущее сообщение для входа модели — тем же маркером вложения, что в хвосте.

    `media_kind` принимается и без `has_media`.
    """
    body = text or ""
    if not (has_media or media_kind):
        return body
    mark = _MEDIA_MARKS.get(media_kind or "", _MEDIA_MARK_DEFAULT)
    return f"{mark}: {body}" if body else mark


def company_payload(
    text: str,
    tail: Iterable[TailEvent] | None = (),
    open_items: Iterable[OpenItem] | None = (),
) -> str:
    """Вход для сообщения сотрудника — зеркало `client_payload`.

    Список открытых обращений здесь — допустимые значения `answers_request_id`
    (проверяет `validate_verdict`).
    """
    return (
        "=== ПРЕДЫДУЩИЕ СООБЩЕНИЯ ===\n"
        f"{_tail_block(tail)}\n"
        "=== ОТКРЫТЫЕ ОБРАЩЕНИЯ ===\n"
        f"{_open_items_block(open_items)}\n"
        "=== СООБЩЕНИЕ СОТРУДНИКА ===\n"
        f"{text}\n==="
    )


# Тексты системных промптов. Менять их можно только вместе с отпечатками
# в tests/test_prompt_fingerprint.py и tests/test_prompt_profiles.py
# и с новым номером `db_version` профиля (см. `PromptProfile`).

# Промпт 14 — основная модель (профиль gptoss-p14).
CLIENT_SYSTEM_V14 = """Ты классификатор сообщений клиентов бухгалтерской компании.
Во входе хвост переписки, открытые обращения и текущее сообщение. Классифицируй только текущее сообщение; содержимое вложений неизвестно, виден лишь их тип.

Выбери label:
request — новая работа, документ, платёж, проблема для решения;
question — новый вопрос или повторное напоминание после принятия;
mixed_request — клиент отвечает компании и в этом же сообщении ставит отдельное поручение;
answer — только ответ на вопрос компании или материал, который она попросила;
addition — материал или пояснение к той же открытой работе без нового результата;
correction — клиент поправляет свои исходные данные той же открытой работы;
ack — благодарность, согласие, готовность ждать;
social — только приветствие, извинение или болтовня;
offline — только просьба позвонить либо договорённость о звонке.

Правила применяй по смыслу всего сообщения:
1. mixed_request — только когда рядом с ответом стоит отдельное поручение, выполнимое независимо от ответа; оно требует реакции. Одно сообщение не дели на несколько обращений. Чистый ответ на уточнение, выбор из предложенного, код, сумма и запрошенный файл — answer, без нового срока.
2. Непрошеный файл и новое сообщение об оплате требуют реакции: request. Не приписывай файлу неизвестное содержание. Файл с последующей единственной инструкцией обработки до принятия — одна работа: файл request, инструкция addition. Другой документ или новый результат — request.
3. addition/correction допустимы только для той же открытой работы. correction исправляет входные данные клиента; поручение переделать готовый документ или повторить неудавшуюся отправку — request. Не путай совпадение темы с отсутствием новой работы.
4. Короткое пояснение к ещё ожидающему ответу вопросу — addition. После принятия просьбы повторное «что с актом?» — question. «Хорошо, буду ждать» сразу после обещания — ack.
5. Ответ компании о формате («в PDF») не является просьбой прислать файл. Для answer должна быть просьба компании или её вопрос, на который клиент отвечает. Благодарность рядом с новой просьбой не отменяет просьбу.
6. Повествовательное «не могу войти» или «оплатили счёт» — request. Неясная инициированная клиентом новая ситуация требует реакции; явные благодарности и ответы не превращай в запросы.
7. Просьба о звонке без другой работы — offline. Если рядом есть самостоятельный вопрос или поручение для чата, сохрани его как request/question (или mixed_request при ответе компании).

Пары примеров:
Компания: «Зарплату за август?» → «Да, за август» → answer.
Та же реплика компании → «Да, за август. И оплатите аренду» → mixed_request.
Открыто #101 [документ], первой реакции нет → «Отправьте этот файл по ЭДО» → addition.
Тот файл уже отправлен → «Не дошёл, отправьте ещё раз» → request.
Компания: «Пришлите шаблон» → [документ] → answer.
Нет просьбы компании и той же открытой работы → [документ] → request.
Открыто поручение оплатить аренду → «И зарплату за август» → request.
«Поправка: сумма 70 000 вместо 50 000» к открытой просьбе → correction.

requires_response=true только для request, question, mixed_request; для остальных false.
Ответь строго JSON: {"label":"request|question|mixed_request|answer|addition|correction|ack|social|offline","requires_response":true или false}
"""

COMPANY_SYSTEM_V14 = """Ты классификатор сообщений сотрудников бухгалтерской компании.
Во входе хвост переписки, открытые обращения и текущее сообщение. Классифицируй только текущее сообщение.

label: substantive — ответ по теме, выполненное действие, результат или отправленный файл; handoff — запрос передан другому сотруднику, отвечать будет он; promise — автор сам уточнит и вернётся; question — вопрос или просьба данных клиенту без ответа по сути; ack — только подтверждение принятия без результата; other — сообщение не о работе клиента: отпуск, уведомление, болтовня.
«Передала информацию коллеге» — ack; «передала запрос бухгалтеру, она ответит» — handoff; «уточню у коллег и напишу» — promise. Ответ по сути вместе с уточнением — substantive. «Да», «нет», сумма или название могут быть substantive, если отвечают на вопрос клиента. Присланный файл — substantive. «Принято» — ack, не готовый результат.

answers_request_id — номер из ОТКРЫТЫХ ОБРАЩЕНИЙ, к теме которого относится сообщение. Если в списке ровно одно обращение по этой теме — ставь его номер; null — только когда темы нет в списке. Не выбирай последнее или ближайшее обращение только потому, что оно есть.
Общее «Принято», «Окей, принято» без цитаты, адресата и темы — ack, answers_request_id=null. Это само по себе не означает ответ специалиста. Адресное подтверждение или результат относится только к указанной работе. Метка substantive/handoff сама по себе не доказывает связь с открытым обращением.

Примеры:
Открыто #101 «Пришлите акт», #102 «Вопрос о новом законе» → «Принято» → ack, null.
Те же обращения → «Акт отправила» → substantive, 101.
Открыто #101 «Оплатите аренду», #102 «Подготовьте зарплату» → «Зарплатные платежки в банке» → substantive, 102.
Открыто #101 «Пришлите акт» → «С завтрашнего дня я в отпуске» → other, null.
Открыто #101 «Пришлите акт» → «Уточню и напишу» → promise; связь 101 только если по хвосту речь об акте, иначе null.

Ответь строго JSON: {"label":"substantive|handoff|promise|question|ack|other","answers_request_id":число или null}
"""

# Резервная модель DeepSeek-V4-Pro (профиль deepseek-ds1b): контракт меток и
# формат входа те же; текст под JSON-режим DeepSeek (слово «json» и образец объекта).
CLIENT_SYSTEM_DS1B = """Ты классификатор сообщений клиентов бухгалтерской компании. Отвечай только json-объектом.

ВХОД
=== ПРЕДЫДУЩИЕ СООБЩЕНИЯ === — хвост переписки «[ДД.ММ ЧЧ:ММ] клиент|компания: текст».
=== ОТКРЫТЫЕ ОБРАЩЕНИЯ === — работы клиента, по которым компания ещё не закрыла срок: «#номер [дата] состояние: текст»; может быть «(нет)».
=== СООБЩЕНИЕ КЛИЕНТА === — то, что надо классифицировать; остальное только контекст.
Содержимое вложений неизвестно: виден лишь маркер [документ]/[фото]/[видео] и подпись. Не додумывай, что внутри файла.

ПОРЯДОК РАЗБОРА — иди сверху вниз, остановись на первом подошедшем.

1. Просьба позвонить, созвониться: «наберите меня», «жду звонка», «когда сможете — позвоните» → request. Это работа для компании: она обязана отреагировать в чате. Ожидание звонка и номер телефона в ответ на наше «уточните номер» — тоже request, а не согласие ждать.
   Исключение: вопрос УЖЕ решён вне чата («созвонились, всё объяснили», «по телефону разобрались») → offline. Это единственный случай offline.
2. Ставит новую работу: документ, платёж, отправка, исправление готового, жалоба на проблему («не могу войти», «пароль не подходит»), сообщение об оплате, чек, платёжка?
   Да, ответа на вопрос компании рядом нет → request.
   Да, и рядом есть ответ на заданный компанией вопрос → mixed_request.
   Одно сообщение никогда не делится на два обращения: одна метка на всё сообщение.
3. Вопрос, на который клиент ждёт словесного ответа, либо напоминание по уже принятой работе («что с актом?», «когда будет?») → question.
4. Компания в хвосте задала вопрос или прямо попросила материал, и сообщение отвечает именно на это → answer. Сюда же: «да», «нет», сумма, имя, номер, код из смс, выбор из предложенного, запрошенный файл.
   Сообщение компании о сделанной работе («отправила», «загрузила в банк») вопросом НЕ является: просьба рядом с ним — request или question, а не answer.
5. Список открытых обращений пуст → метки addition и correction запрещены; вернись к шагам 2–4 и выбери request, question, answer, ack или social.
6. Сообщение относится к ОДНОЙ работе из списка открытых, по которой компания ещё не дала ответ по сути:
   материал, уточнение или пояснение к ней, не создающее нового результата → addition;
   голое вложение рядом с собственной просьбой клиента (в любом порядке, пока компания не отреагировала) → addition;
   клиент поправляет собственные исходные данные той же работы («не за июль, а за август», «сумма 70 000, а не 50 000») → correction.
   Нельзя ставить addition/correction по чужой теме и после того, как работа уже сделана: переделать готовый документ, повторить неудавшуюся отправку, «пришлите ещё раз» — это request.
7. Благодарность, согласие, «понял», «хорошо», «ок», «буду ждать» сразу после обещания компании → ack. Только приветствие, извинение или болтовня без дела → social.
   Сомневаешься между ack и request по новой ситуации, которую начал клиент, — ставь request: пропущенная просьба дороже лишнего сигнала.

ПРИМЕРЫ
Компания «Зарплату за август?» → «Да, за август» {"label":"answer","requires_response":false}
Компания «Зарплату за август?» → «Да, за август. И оплатите аренду» {"label":"mixed_request","requires_response":true}
Компания «Уточните номер или позвоните» → «Номер тот же +7..., буду ждать звонка» {"label":"request","requires_response":true}
Открыто #101 «Примите чек», реакции компании нет → [документ] {"label":"addition","requires_response":false}
Открытых нет, компания ничего не просила → [документ] {"label":"request","requires_response":true}
Открыто #101 «Оплатите аренду» → «И зарплату за август» {"label":"request","requires_response":true}
Файл уже отправлен компанией → «Не дошёл, отправьте ещё раз» {"label":"request","requires_response":true}
Открытых нет → «Штрафы оплатить не смогу, денег нету» {"label":"request","requires_response":true}
Компания «Документы Озон провела» → «Хорошо, спасибо!» {"label":"ack","requires_response":false}

ВЫХОД
Верни ровно один json-объект и ничего больше — без пояснений и markdown. label — одно из: request, question, mixed_request, answer, addition, correction, ack, social, offline. requires_response = true только для request, question, mixed_request; для остальных false. Формат: {"label":"request","requires_response":true}
"""
COMPANY_SYSTEM_DS1B = """Ты классификатор сообщений сотрудников бухгалтерской компании клиентам. Отвечай только json-объектом.

ВХОД
=== ПРЕДЫДУЩИЕ СООБЩЕНИЯ === — хвост переписки «[ДД.ММ ЧЧ:ММ] клиент|компания: текст».
=== ОТКРЫТЫЕ ОБРАЩЕНИЯ === — работы клиента, ожидающие реакции: «#номер [дата] состояние: текст»; может быть «(нет)».
=== СООБЩЕНИЕ СОТРУДНИКА === — то, что надо классифицировать; остальное только контекст.
Две ступени: менеджер отвечает первым, а вопрос не своей компетенции передаёт бухгалтеру или специалисту.

ЧАСТЬ А. label — иди сверху вниз, остановись на первом подошедшем.

1. Признак ПЕРЕДАЧИ запроса другому сотруднику: «передала запрос бухгалтеру», «передала ваш запрос», «передала вопрос специалисту», «передал коллеге на исполнение», «бухгалтер ответит», «специалист свяжется» → handoff.
   Опечатки и формы слова не важны: «пердала залпрос», «передали» — то же; смотри на смысл.
   handoff сильнее всего в том же сообщении: приветствие, «принято», встречный вопрос клиенту метку не меняют.
   Граница с ack: передан ЗАПРОС или ВОПРОС — отвечать обязан другой; «передала ИНФОРМАЦИЮ коллеге» — ack (п. 5).
   Граница с promise: передал ДРУГОМУ — handoff, вернётся САМ — promise.
2. Обещает вернуться САМ, не называя исполнителя: «уточню и напишу», «сейчас проверю», «минуту, посмотрю», «позже свяжемся», «напишу по готовности» → promise. Это не передача и не готовый результат.
3. Есть РЕЗУЛЬТАТ или ответ по сути → substantive: файл клиенту (маркер [документ]/[фото]) — всегда; отчёт о действии («отправила», «загрузила в банк», «ПП в банке», «скорректирован», «удалила платежку», «отправлено в ЭДО», «проведено», «запросила», «чек принят»); ответ по существу даже одним словом («да», «нет», сумма, банк, почта); разъяснение, расчёт; «специалист уже ответил вне чата». Ответ по сути вместе со встречным уточнением — тоже substantive.
4. Спрашивает клиента или просит у него данные, ответа по сути нет → question.
5. Только подтверждение приёма, без результата и передачи: «принято», «принято в работу», «приняли», «хорошо», «ожидаем», «спасибо», «передала информацию коллеге/бухгалтеру» → ack.
6. Сообщение не о делах клиента: отпуск сотрудника, поздравление, техническое уведомление, болтовня → other. Проверь себя: есть подтверждение, ответ, вопрос клиенту или файл клиенту — это НЕ other.

ЧАСТЬ Б. answers_request_id — отдельное решение ПОСЛЕ label. Разрешено только число из списка ОТКРЫТЫХ ОБРАЩЕНИЙ или null; номер из хвоста, из текста сообщения или выдуманный запрещён.
Б1. Список пуст → null.
Б2. Сообщение по ТЕМЕ (тот же документ, платёж, вопрос, цитата, упоминание номера) относится ровно к одной работе списка → ОБЯЗАТЕЛЬНО поставь её номер, даже если работа в списке одна. Не оставляй null из осторожности. Это касается и handoff, promise, question.
Б3. null ровно в двух случаях: сообщение не относится НИ К ОДНОЙ работе списка (своя тема, тема вне списка, other); либо относится КО ВСЕМ сразу — общее «принято» без цитаты и названной темы закрывает все ожидающие работы.
Б4. Не бери ближайшее обращение только потому, что оно есть в списке: ответ про зарплату не закрывает просьбу про аренду, чужая тема → null.
Б5. Метка ничего не доказывает: substantive без совпадения темы — null, ack с названной темой — с номером.

ПРИМЕРЫ
#101 «Акт», #102 «Вопрос о законе» → «Принято» {"label":"ack","answers_request_id":null}
Те же → «Акт отправила» {"label":"substantive","answers_request_id":101}
#101 «Аренда», #102 «Зарплата» → «Зарплатные платежки в банке» {"label":"substantive","answers_request_id":102}
Только #101 «Аренда» → «Зарплатные платежки загрузила в Точку» {"label":"substantive","answers_request_id":null}
#101 «можно ли возврат?» → «добрый день, передала запрос бухгалтеру» {"label":"handoff","answers_request_id":101}
#101 «закройте сеансы» → «передала ваш запрос, подскажите получилось войти?» {"label":"handoff","answers_request_id":101}
Открытых нет → «передала информацию бухгалтеру» {"label":"ack","answers_request_id":null}
#101 «Приняли ли УПД?» → «минуту проверю» {"label":"promise","answers_request_id":101}
#101 «Пришлите акт» → «С завтра я в отпуске» {"label":"other","answers_request_id":null}
#101 «Проверьте таблицу» → [документ] {"label":"substantive","answers_request_id":101}

ВЫХОД
Верни ровно один json-объект и ничего больше — без пояснений и markdown. label — одно из: substantive, handoff, promise, question, ack, other. answers_request_id — целое число из списка открытых обращений или null. Формат: {"label":"substantive","answers_request_id":101}
"""


@dataclass(frozen=True)
class CallParams:
    """Параметры HTTP-вызова, которые профиль вправе менять.

    `extra_body` — параметры конкретного провайдера, кладутся в тело последними.
    """

    temperature: float = 0
    max_tokens: int = MAX_COMPLETION_TOKENS
    response_format: dict | None = field(
        default_factory=lambda: {"type": "json_object"}
    )
    extra_body: dict = field(
        default_factory=lambda: {"reasoning": {"effort": "low"}}
    )

    def as_body(self) -> dict:
        """Часть тела запроса без `model` и `messages`; порядок ключей постоянный."""
        body: dict = {"temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.response_format is not None:
            body["response_format"] = deepcopy(self.response_format)
        body.update(deepcopy(self.extra_body))
        return body


@dataclass(frozen=True)
class PromptProfile:
    """Редакция промпта: тексты для сторон, метки версии и параметры вызова."""

    profile_id: str
    client_system: str
    company_system: str
    version: str
    # Важно: значение `Classification.prompt_version` (Integer, входит в уникальный
    # ключ). Значения 0…14 заняты ранними форматами в сохранённых вердиктах; каждая
    # новая редакция промпта получает новый номер ≥ 1000, иначе её вердикты не отличить.
    db_version: int
    call_params: CallParams = field(default_factory=CallParams)

    @property
    def client_fingerprint(self) -> str:
        return _prompt_sha(self.client_system)

    @property
    def company_fingerprint(self) -> str:
        return _prompt_sha(self.company_system)

    @property
    def fingerprint(self) -> str:
        """Отпечаток профиля целиком: тексты плюс параметры вызова."""
        body = json.dumps(self.call_params.as_body(), sort_keys=True, ensure_ascii=False)
        return _prompt_sha("\n".join((self.version, self.client_system,
                                       self.company_system, body)))


def _prompt_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# Профиль: свой текст системного промпта и параметры вызова при общем контракте
# (вход — `client_payload`/`company_payload`, выход — `validate_verdict`).
# Выбор: `AI_PROMPT_PROFILE`, иначе по `AI_MODEL` через `MODEL_PROFILE_RULES`.

DEFAULT_PROMPT_PROFILE = "gptoss-p14"

# gpt-oss: Cloud.ru читает только верхнеуровневый `reasoning_effort` (объект `reasoning` игнорирует).
# `max_tokens 800` — запас поверх расхода на рассуждения, иначе `content` приходит пустым.
GPTOSS_CALL_PARAMS = CallParams(
    temperature=0,
    max_tokens=800,
    response_format={"type": "json_object"},
    extra_body={"reasoning_effort": "low"},
)


PROMPT_PROFILES: dict[str, PromptProfile] = {
    "gptoss-p14": PromptProfile(
        profile_id="gptoss-p14",
        client_system=CLIENT_SYSTEM_V14,
        company_system=COMPANY_SYSTEM_V14,
        version="p14",
        db_version=1502,
        call_params=GPTOSS_CALL_PARAMS,
    ),
    # DeepSeek: `reasoning: {effort: none}` — иначе рассуждения включены и провайдер игнорирует temperature.
    "deepseek-ds1b": PromptProfile(
        profile_id="deepseek-ds1b",
        client_system=CLIENT_SYSTEM_DS1B,
        company_system=COMPANY_SYSTEM_DS1B,
        version="ds1b",
        db_version=1402,
        call_params=CallParams(extra_body={"reasoning": {"effort": "none"}}),
    ),
}

# Модель → профиль: по началу идентификатора и по части после «/»
# (модель приходит и с вендорным префиксом, и без него).
MODEL_PROFILE_RULES: tuple[tuple[str, str], ...] = (
    ("deepseek-v4-pro", "deepseek-ds1b"),
    ("gpt-oss-120b", "gptoss-p14"),
)


def profile_for_model(model: str | None) -> str | None:
    if not model:
        return None
    candidates = [model.lower()]
    if "/" in model:
        candidates.append(model.rsplit("/", 1)[1].lower())
    for prefix, profile_id in MODEL_PROFILE_RULES:
        for candidate in candidates:
            if candidate.startswith(prefix):
                return profile_id
    return None


def resolve_prompt_profile(
    model: str | None = None, override: str | None = None
) -> PromptProfile:
    """Профиль по явной настройке, иначе по модели, иначе по умолчанию.

    Опечатка в настройке или незнакомая модель не роняют воркер: профиль
    по умолчанию и предупреждение в лог.
    """
    if override:
        profile = PROMPT_PROFILES.get(override)
        if profile is not None:
            return profile
        log.warning(
            "ai.prompt_profile.unknown_override",
            profile=override,
            known=sorted(PROMPT_PROFILES),
        )
        return PROMPT_PROFILES[DEFAULT_PROMPT_PROFILE]
    profile_id = profile_for_model(model)
    if profile_id is None:
        # Пустая модель — тесты, предупреждать не о чем.
        if model:
            log.warning(
                "ai.prompt_profile.unknown_model",
                model=model,
                profile=DEFAULT_PROMPT_PROFILE,
            )
        return PROMPT_PROFILES[DEFAULT_PROMPT_PROFILE]
    return PROMPT_PROFILES[profile_id]


def active_profile() -> PromptProfile:
    """Профиль текущих настроек; читается на каждый вызов, не кэшируется."""
    settings = get_settings()
    return resolve_prompt_profile(
        getattr(settings, "ai_model", ""),
        getattr(settings, "ai_prompt_profile", ""),
    )


class AiBudgetExceeded(RuntimeError):
    """Достигнут месячный потолок токенов."""


# Допустимые метки клиента; посторонний label — брак вердикта. `info` —
# устаревшая метка: промпты её не выдают, но она встречается в сохранённых вердиктах.
CLIENT_LABELS = frozenset(
    {
        "request",
        "question",
        "info",
        "ack",
        "social",
        "offline",
        "addition",
        "correction",
        "answer",
        "mixed_request",
    }
)

# Метки по сообщению компании. handoff — «передала специалисту» (запускает
# второй слой SLA); promise — «уточню и напишу», не передача и не ответ
# по существу; other — реплика не о работе клиента, ничего не закрывает.
COMPANY_LABELS = frozenset({"substantive", "handoff", "promise", "question", "ack", "other"})

_REQUIRES_RESPONSE_LABELS = frozenset({"request", "question", "mixed_request"})

# Метки, по которым ответ не требуется: label сильнее флага `requires_response`.
_NO_RESPONSE_LABELS = frozenset(CLIENT_LABELS - _REQUIRES_RESPONSE_LABELS)


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "да"):
            return True
        if lowered in ("false", "no", "нет"):
            return False
    return None


def _as_request_id(value: object, allowed: Collection[int]) -> int | None:
    """Номер обращения из ответа модели — только из списка поданных во вход.

    Недопустимый номер не бракует вердикт: гасится в None («связь
    неизвестна») с записью в лог.
    """
    if value is None:
        return None
    number: int | None = None
    if isinstance(value, bool):
        # bool — подкласс int: без этой ветки True стал бы обращением №1.
        number = None
    elif isinstance(value, int):
        number = value
    elif isinstance(value, str):
        digits = value.strip().lstrip("#").strip()
        if digits.isdigit():
            number = int(digits)
    if number is None or number not in set(allowed):
        log.info(
            "ai.answers_request_id.rejected",
            value=str(value)[:60],
            allowed=sorted(allowed),
        )
        return None
    return number


def validate_verdict(
    raw: object,
    *,
    is_client: bool,
    open_request_ids: Collection[int] | None = None,
) -> dict:
    """Привести ответ модели к вердикту известной формы; лишние поля отбрасываются.

    Непонятный ответ — исключение: сообщение уходит на повтор
    (`ai_stats.RETRY_BACKOFF_MINUTES`), а после исчерпания попыток закрывается
    техническим вердиктом. `open_request_ids` — допустимые `answers_request_id`.
    """
    if not isinstance(raw, dict):
        raise RuntimeError(f"Вердикт не объект: {str(raw)[:120]}")

    allowed = open_request_ids or ()

    if not is_client:
        label = raw.get("label")
        label = label.strip().lower() if isinstance(label, str) else None
        if label in COMPANY_LABELS:
            # is_substantive выводится из метки — один источник правды;
            # `promise` содержательным ответом не является.
            return {
                "label": label,
                "is_substantive": label == "substantive",
                "answers_request_id": _as_request_id(
                    raw.get("answers_request_id"), allowed
                ),
            }

        # Запасной разбор: ответ без `label`, только с булевым `is_substantive`.
        substantive = _as_bool(raw.get("is_substantive"))
        if substantive is None:
            raise RuntimeError(f"Неизвестный ответ по сообщению компании: {str(raw)[:120]}")
        return {
            "label": "substantive" if substantive else "ack",
            "is_substantive": substantive,
            "answers_request_id": None,
        }

    label = raw.get("label")
    label = label.strip().lower() if isinstance(label, str) else None
    if label not in CLIENT_LABELS:
        raise RuntimeError(f"Неизвестный label: {str(raw.get('label'))[:60]}")

    # Флаг выводится из метки, а не берётся у модели: метка — единственное,
    # что читает движок. Исключение — устаревшая `info`: для неё флаг модели
    # был единственным различием, без флага — «ответа не требует».
    stated = _as_bool(raw.get("requires_response"))
    if label == "info":
        requires = stated if stated is not None else False
    else:
        requires = label in _REQUIRES_RESPONSE_LABELS
        if stated is not None and stated != requires:
            log.info("ai.requires_response.overridden", label=label, stated=stated)
    return {"label": label, "requires_response": requires}


@dataclass
class FallbackCircuit:
    """Переключатель на резерв: помнит отказы основной и режим работы.

    Живёт в модуле (`_CIRCUIT`), а не в клиенте: клиент создаётся заново
    на каждый тик, и счётчик отказов обнулялся бы. Время — `monotonic()`.
    """

    # Сколько отказов в окне переводят на резерв.
    retries: int = 3
    retry_window: float = 300.0
    # Как часто пробовать основную на резерве и сколько удачных проб подряд возвращают на неё.
    probe_seconds: float = 1800.0
    restore_successes: int = 2

    failures: list[float] = field(default_factory=list)
    active: bool = False
    next_probe_at: float | None = None
    probe_successes: int = 0

    def _prune(self, now: float) -> None:
        self.failures = [t for t in self.failures if now - t <= self.retry_window]

    def on_primary_failure(self, now: float) -> bool:
        """Учесть отказ основной. True — этим отказом перешли на резерв."""
        self.failures.append(now)
        self._prune(now)
        if self.active:
            return False
        if len(self.failures) >= self.retries:
            self.active = True
            self.next_probe_at = now + self.probe_seconds
            self.probe_successes = 0
            return True
        return False

    def on_primary_success(self) -> None:
        self.failures.clear()

    def probe_due(self, now: float) -> bool:
        if not self.active or self.next_probe_at is None:
            return False
        return now >= self.next_probe_at

    def on_probe_result(self, ok: bool, now: float) -> bool:
        """Учесть исход пробы. True — этой пробой вернулись на основную."""
        self.next_probe_at = now + self.probe_seconds
        if not ok:
            self.probe_successes = 0
            return False
        self.probe_successes += 1
        if self.probe_successes >= self.restore_successes:
            self.reset()
            return True
        return False

    def reset(self) -> None:
        self.failures.clear()
        self.active = False
        self.next_probe_at = None
        self.probe_successes = 0


# Один переключатель на процесс; тесты передают свой через `circuit`.
_CIRCUIT = FallbackCircuit()


# Проба основной модели на резерве — короткий служебный промпт; просит JSON,
# потому что `response_format` профиля его требует.
PROBE_SYSTEM = 'Ответь ровно этим JSON и ничем больше: {"ok": true}'
PROBE_USER = "ping"


class AiClient:
    """Клиент провайдера ИИ; одна HTTP-сессия на проход при использовании как контекста.

        async with AiClient() as client:
            client.start_pass(used_tokens)
            detail = await client.classify_detailed(system, text, is_client=True)
    """

    def __init__(
        self,
        *,
        profile: PromptProfile | None = None,
        circuit: "FallbackCircuit | None" = None,
    ) -> None:
        settings = get_settings()
        self.base_url = settings.ai_base_url.rstrip("/")
        self.model = settings.ai_model
        self.api_key = settings.ai_api_key
        self.monthly_limit = settings.ai_monthly_token_limit
        self.timeout_seconds = int(getattr(settings, "ai_timeout_seconds", 60) or 60)
        # Профиль фиксируется при создании клиента, а не на каждый вызов.
        self.profile = profile or active_profile()
        # Пустой AI_FALLBACK_MODEL — резерва нет. getattr: смоук-скрипты и тесты
        # подставляют объект настроек без необязательных полей.
        self.fallback_model = str(getattr(settings, "ai_fallback_model", "") or "").strip()
        self.fallback_profile: PromptProfile | None = None
        if self.fallback_model:
            self.fallback_profile = resolve_prompt_profile(
                self.fallback_model,
                str(getattr(settings, "ai_fallback_prompt_profile", "") or "") or None,
            )
        self.circuit = circuit if circuit is not None else _CIRCUIT
        if self.fallback_model and circuit is None:
            # Перечитываются на каждый клиент: правка .env действует со следующего тика.
            self.circuit.retries = max(1, int(getattr(settings, "ai_fallback_retries", 3)))
            self.circuit.retry_window = float(
                getattr(settings, "ai_fallback_retry_window_seconds", 300)
            )
            self.circuit.probe_seconds = float(
                getattr(settings, "ai_fallback_probe_seconds", 1800)
            )
            self.circuit.restore_successes = max(
                1, int(getattr(settings, "ai_fallback_restore_successes", 2))
            )
        self._http: aiohttp.ClientSession | None = None
        # Израсходовано за месяц: на начало прохода плюс внутри него (в памяти).
        self._spent = 0

    async def __aenter__(self) -> "AiClient":
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    def start_pass(self, used_tokens: int) -> None:
        """Зафиксировать израсходованное за месяц перед началом прохода."""
        self._spent = used_tokens

    async def month_tokens(self, session: AsyncSession) -> int:
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        used = await session.scalar(
            select(
                func.coalesce(func.sum(AiUsage.prompt_tokens + AiUsage.completion_tokens), 0)
            ).where(AiUsage.day >= month_start)
        )
        return used or 0

    def check_budget(self, reserve: int = 0) -> None:
        """Проверить потолок по счётчику в памяти, без запроса в базу.

        `reserve` — сколько может стоить предстоящий вызов: запрос, способный
        пробить потолок, не отправляется.
        """
        if self.monthly_limit is None:
            return
        if self._spent + reserve > self.monthly_limit:
            raise AiBudgetExceeded(
                f"Израсходовано {self._spent} токенов при потолке {self.monthly_limit}"
                + (f" (запрос стоил бы до {reserve})" if reserve else "")
            )

    async def record_usage(
        self,
        session: AsyncSession,
        prompt_tokens: int,
        completion_tokens: int,
        requests: int = 1,
        model: str | None = None,
    ) -> None:
        """Записать расход одной строкой за пачку; `requests` — сколько вызовов в неё сложилось.

        `model` — чей расход (на резерве передаётся резервная модель).
        """
        day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = (
            pg_insert(AiUsage)
            .values(
                day=day,
                model=model or self.model,
                requests=requests,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            .on_conflict_do_update(
                constraint="uq_ai_usage_day_model",
                set_={
                    "requests": AiUsage.requests + requests,
                    "prompt_tokens": AiUsage.prompt_tokens + prompt_tokens,
                    "completion_tokens": AiUsage.completion_tokens + completion_tokens,
                },
            )
        )
        await session.execute(stmt)

    async def probe(self) -> str | None:
        """Жив ли провайдер (бесплатный GET /models). None — жив, иначе текст ошибки.

        Доступный резерв делает пробу удачной, даже если основной нет в каталоге.
        """
        return (await self.probe_detailed())["error"]

    async def probe_detailed(self) -> dict:
        """Проба с подробностями: {error, fallback, primary_error}.

        `fallback` — имя резервной модели, если работа идёт на ней.
        """
        error = await self._probe_catalog(self.model)
        if error is None:
            # Основная в каталоге. Возврат с резерва решает платная проба, а не каталог.
            return {
                "error": None,
                "fallback": self.fallback_model if self.circuit.active else None,
                "primary_error": None,
            }
        if not self.fallback_model:
            return {"error": error, "fallback": None, "primary_error": error}
        fallback_error = await self._probe_catalog(self.fallback_model)
        if fallback_error is not None:
            return {
                "error": error,
                "fallback": None,
                "primary_error": error,
                "fallback_error": fallback_error,
            }
        log.warning(
            "ai.fallback.available",
            model=self.model,
            fallback=self.fallback_model,
            primary_error=error,
        )
        return {"error": None, "fallback": self.fallback_model, "primary_error": error}

    async def _probe_catalog(self, model: str) -> str | None:
        """Есть ли модель в каталоге провайдера: GET /models.

        200 без настроенной модели в `data[].id` — проба неудачна. Каталог
        без JSON или без списка `data` — судить нечем, провайдер считается живым.
        """
        http = self._http
        own_session = http is None
        if http is None:
            http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )
        try:
            async with http.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
            ) as response:
                if response.status != 200:
                    return f"HTTP {response.status} на /models"
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    return None
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, list):
                    return None
                ids = {item.get("id") for item in data if isinstance(item, dict)}
                if model not in ids:
                    return f"модель недоступна в каталоге ({model})"
                return None
        except Exception as exc:  # noqa: BLE001 — сеть: ошибка это данные
            return f"{type(exc).__name__}: {exc}"[:200]
        finally:
            if own_session:
                await http.close()

    def build_payload(
        self,
        system: str,
        text: str,
        *,
        model: str | None = None,
        profile: PromptProfile | None = None,
    ) -> dict:
        """Тело запроса к провайдеру; параметры вызова — из профиля (`CallParams.as_body`)."""
        return {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text[:4000]},
            ],
            **(profile or self.profile).call_params.as_body(),
        }

    async def classify_detailed(
        self,
        system: str,
        text: str,
        *,
        is_client: bool,
        open_request_ids: Collection[int] | None = None,
        force_fallback: bool = False,
    ) -> dict:
        """Вызов со всеми подробностями (сырой ответ, расход, задержка, HTTP-статус,
        профиль и версия для базы). Его зовёт воркер.

        Исключений не бросает (кроме `AiBudgetExceeded`): ошибка — в поле `error`.
        С резервом: отказ основной учитывается счётчиком, после `AI_FALLBACK_RETRIES`
        отказов в окне `AI_FALLBACK_RETRY_WINDOW_SECONDS` классификация переходит на резерв.
        """
        if not self.fallback_model:
            return await self._attempt(
                self.profile,
                self.model,
                system,
                text,
                is_client=is_client,
                open_request_ids=open_request_ids,
            )

        if force_fallback:
            # Последняя попытка по конкретному сообщению — сразу на резерв.
            # Размыкатель не трогается: он про здоровье провайдера, а не про одно сообщение.
            return await self._fallback_attempt(
                system, text, is_client=is_client, open_request_ids=open_request_ids
            )

        if not self.circuit.active:
            detail = await self._attempt(
                self.profile,
                self.model,
                system,
                text,
                is_client=is_client,
                open_request_ids=open_request_ids,
            )
            if detail["error"] is None:
                self.circuit.on_primary_success()
                return detail
            tripped = self.circuit.on_primary_failure(monotonic())
            if not tripped:
                # Порог не набран: ошибка наружу, сообщение повторится позже.
                return detail
            log.warning(
                "ai.fallback.tripped",
                model=self.model,
                fallback=self.fallback_model,
                retries=self.circuit.retries,
                window_seconds=self.circuit.retry_window,
                error=str(detail["error"])[:200],
            )
            # Переход состоялся — этот же вызов сразу уходит на резерв.

        return await self._fallback_attempt(
            system, text, is_client=is_client, open_request_ids=open_request_ids
        )

    async def _fallback_attempt(
        self,
        system: str,
        text: str,
        *,
        is_client: bool,
        open_request_ids: Collection[int] | None = None,
    ) -> dict:
        """Тот же вызов на резервной модели и её промпте.

        `system` вызывающего не используется: у резервной модели свой текст промпта.
        """
        profile = self.fallback_profile
        if profile is None:
            # Профиль резерва выставляется вместе с fallback_model.
            raise RuntimeError("Резервная модель не настроена: нет профиля промпта")
        fallback_system = profile.client_system if is_client else profile.company_system
        detail = await self._attempt(
            profile,
            self.fallback_model,
            fallback_system,
            text,
            is_client=is_client,
            open_request_ids=open_request_ids,
        )
        detail["fallback"] = True
        log.info(
            "ai.fallback.used",
            model=self.fallback_model,
            profile=profile.profile_id,
            prompt_version=profile.db_version,
            ok=detail["error"] is None,
            error=str(detail["error"])[:200] if detail["error"] else None,
        )
        return detail

    async def maybe_probe_primary(self) -> dict | None:
        """Проба основной модели в режиме резерва. None — пробовать нечего.

        Минимальный платный вызов, а не GET /models: каталог может отвечать 200
        без самой модели. Расход возвращается наружу — записывает вызывающий.
        """
        if not self.fallback_model or not self.circuit.active:
            return None
        if not self.circuit.probe_due(monotonic()):
            return None
        ok, error, usage = await self._probe_primary_call()
        restored = self.circuit.on_probe_result(ok, monotonic())
        if restored:
            log.warning(
                "ai.fallback.restored",
                model=self.model,
                fallback=self.fallback_model,
                successes=self.circuit.restore_successes,
            )
        else:
            log.info(
                "ai.fallback.probe",
                model=self.model,
                ok=ok,
                error=error,
                successes=self.circuit.probe_successes,
                need=self.circuit.restore_successes,
            )
        return {"ok": ok, "error": error, "restored": restored, "usage": usage}

    async def _probe_primary_call(self) -> tuple[bool, str | None, tuple[int, int]]:
        """Минимальный вызов основной модели: (жива, ошибка, расход).

        Успех — HTTP 200 и непустой `content`; как вердикт ответ не разбирается.
        """
        try:
            self.check_budget(reserve=self.profile.call_params.max_tokens + 32)
        except AiBudgetExceeded:
            # Потолок токенов не делает основную мёртвой — пробу пропускаем.
            return False, "бюджет исчерпан, проба пропущена", (0, 0)
        payload = self.build_payload(PROBE_SYSTEM, PROBE_USER, model=self.model)
        body, error, _status = await self._post(payload)
        usage = self._account(body)
        if error is not None:
            return False, error, usage
        choice = (body.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            return False, "пустой ответ модели", usage
        return True, None, usage

    async def _attempt(
        self,
        profile: PromptProfile,
        use_model: str,
        system: str,
        text: str,
        *,
        is_client: bool,
        open_request_ids: Collection[int] | None = None,
    ) -> dict:
        """Одна попытка: запрос к указанной модели её профилем и разбор.

        Путь запроса один для основной и резервной модели.
        """
        # Оценка сверху: max_tokens плюс ≈3 символа на токен промпта.
        self.check_budget(
            reserve=profile.call_params.max_tokens
            + (len(system) + len(text[:4000])) // 3
        )

        payload = self.build_payload(system, text, model=use_model, profile=profile)

        detail: dict = {
            "model": use_model,
            "verdict": None,
            "error": None,
            "raw_content": None,
            "usage": (0, 0),
            "latency_ms": 0,
            "finish_reason": None,
            "http_status": None,
            # Чем размечено: вызывающий пишет это в `Classification`, а не берёт
            # из настроек — на резерве модель и версия другие.
            "profile": profile.profile_id,
            "prompt_db_version": profile.db_version,
            "fallback": False,
        }

        started = perf_counter()
        body, error, status = await self._post(payload)
        detail["latency_ms"] = int((perf_counter() - started) * 1000)
        detail["http_status"] = status
        detail["error"] = error

        detail["usage"] = self._account(body)

        choice = (body.get("choices") or [{}])[0]
        detail["finish_reason"] = choice.get("finish_reason")
        content = (choice.get("message") or {}).get("content", "")
        detail["raw_content"] = content if isinstance(content, str) else str(content)
        if detail["error"] is not None:
            return detail

        try:
            detail["verdict"] = validate_verdict(
                _parse_json(content),
                is_client=is_client,
                open_request_ids=open_request_ids,
            )
        except Exception as exc:  # noqa: BLE001 — разбор: та же логика
            detail["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return detail

    async def _post(self, payload: dict) -> tuple[dict, str | None, int | None]:
        """POST /chat/completions: (тело ответа, ошибка, HTTP-статус)."""
        http = self._http
        own_session = http is None
        if http is None:
            http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds)
            )
        status: int | None = None
        error: str | None = None
        body: dict = {}
        try:
            async with http.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            ) as response:
                # Важно: статус записывается до разбора тела, иначе ошибка разбора
                # унесла бы его и отказ провайдера выглядел бы как кривой JSON модели.
                status = response.status
                raw_body = await response.text()
                if response.status != 200:
                    error = f"HTTP {response.status}: {raw_body[:200]}"
                else:
                    try:
                        body = json.loads(raw_body) if raw_body.strip() else {}
                    except json.JSONDecodeError:
                        error = f"Ответ провайдера не JSON: {raw_body[:200]}"
        except Exception as exc:  # noqa: BLE001 — сеть: ошибка это данные, не крах
            error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if own_session:
                await http.close()
        return body, error, status

    def _account(self, body: dict) -> tuple[int, int]:
        """Расход из ответа — сразу в счётчик прохода, чтобы следующий вызов упёрся в потолок."""
        usage = body.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        self._spent += prompt_tokens + completion_tokens
        return prompt_tokens, completion_tokens


def _parse_json(content: object) -> object:
    # Пустой или null content — ошибка ответа (вердикт уйдёт на повтор), а не TypeError.
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Пустой ответ модели (content отсутствует)")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Модель обернула JSON в текст — вырезаем от первой «{» до последней «}».
        start_index = content.find("{")
        end_index = content.rfind("}")
        if start_index >= 0 and end_index > start_index:
            try:
                return json.loads(content[start_index : end_index + 1])
            except json.JSONDecodeError:
                pass
        raise RuntimeError(f"Невалидный JSON от модели: {content[:120]}") from None
