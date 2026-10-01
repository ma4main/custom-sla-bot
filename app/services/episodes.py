"""Движок эпизодов: из потока сообщений — обращения и ответы на них.

Обращения пересобираются целиком из сообщений и классификаций при каждом проходе
воркера. Правила и версии v1–v4 — docs/ARCHITECTURE.md §7.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from types import MappingProxyType
from typing import Mapping

import structlog
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.calendar import (
    CalendarHistory,
    business_seconds,
    next_working_moment,
    response_deadline,
    same_time_next_workday,
)
from app.db.models import (
    Attribution,
    BusinessSide,
    Chat,
    ChatTrackingPeriod,
    Classification,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.attribution import not_staff_condition, strip_integrator_prefix
from app.services.broadcasts import broadcast_message_ids
from app.services.settings_store import (
    DEFAULT_REACTION_MINUTES,
    DEFAULT_SPECIALIST_MINUTES,
    DEFAULT_WAIT_REACTION_HOURS,
    DEFAULT_WAIT_SPECIALIST_DAYS,
    get_section,
    versioned_fields,
)
from app.services.tracking import observed_filter
from app.services.transport_notices import is_integrator_notice
from app.services.verdicts import SOURCE_RULE, SOURCE_TECHNICAL
from app.services.transcript import SUBSTANTIVE_MEDIA
from app.services.versioning import History

log = structlog.get_logger(__name__)

# Последняя версия правил; у обращения своя `Interaction.version` по границам
# EPISODE_RULES_V2/V3/V4_SINCE.
ENGINE_VERSION = 4

# Метка редакции правил движка; выводится в лог старта воркера.
ENGINE_RULES_REVISION = "2026-09-27"

# Метки клиента, относящие реплику к уже открытому обращению:
# поправка до первой реакции сдвигает срок, дополнение — нет.
LABEL_ADDITION = "addition"
LABEL_CORRECTION = "correction"
LABEL_MIXED_REQUEST = "mixed_request"

# Просьба о звонке: внутри окна ожидания ответа клиента открывает обращение.
# Стеблей «звон»/«звони» нет намеренно: они ловят «дозвониться» — жалобу, а не просьбу.
CALL_REQUEST_MARKERS = (
    "позвон", "перезвон", "созвон", "набер", "звонк", "звонок",
)

# «Свяжитесь/свяжись» вскрывает окно ожидания, как просьба позвонить. Только повелительные
# формы: изъявительные («связались», «на связи») — рассказ, а не просьба, поэтому общего
# стебля «связ» нет.
CONTACT_REQUEST_MARKERS = ("свяжит", "свяжись")

# Поручение внутри окна ожидания ответа клиента. Повелительное 2-го лица кончается на
# «-ите/-йте/-ьте» (+ «-сь»); стоп-список — те же формы, не являющиеся поручением.
_IMPERATIVE_RE = re.compile(r"\b[а-яё]{2,}?(?:ите|йте|ьте)(?:сь)?\b", re.I)
_IMPERATIVE_STOP = frozenset({
    "здравствуйте", "извините", "простите", "хотите", "видите",
    "говорите", "считаете", "думаете", "смотрите", "понимаете",
    "помните", "знаете", "будьте",
})
_POLITE_MARKERS = ("пожалуйста", "пожалуста", "пжл", "прошу", "просьба")
# «Давайте» в окне ожидания — согласие на предложение компании («Да, давайте сделаем»,
# «давайте в битрикс», «Ну давайте не будем»), а не поручение: `instruction` его не считает.
# «Давай» под `_IMPERATIVE_RE` не попадает и так.
_CONSENT_IMPERATIVES = frozenset({"давайте"})
_NEED_RE = re.compile(r"\b(?:нужно|надо|необходимо|требуется)\b", re.I)
_INFINITIVE_RE = re.compile(r"\b[а-яё]{3,}(?:ть|ться|чь)\b", re.I)

# Вопрос клиента без вопросительного знака.
_QUESTION_WORDS = (
    "почему", "зачем", "сколько", "каким образом", "как так",
    "подскажите", "распишите", "поясните", "объясните", "разъясните",
    "хочется понимать", "хотим понимать", "хотелось бы понять",
    "хочу понять", "непонятно", "не понятно", "неясно", "не ясно",
)
_QUESTION_WORD_RE = re.compile(
    "|".join(re.escape(word) for word in _QUESTION_WORDS), re.I
)


def _imperatives(text: str) -> list[str]:
    found = []
    for match in _IMPERATIVE_RE.finditer(text):
        word = match.group(0).lower()
        if word not in _IMPERATIVE_STOP:
            found.append(word)
    return found


def polite_instruction(text: str) -> bool:
    """Вежливая форма и повелительное наклонение («пришлите, пожалуйста»)."""
    low = text.lower()
    return any(word in low for word in _POLITE_MARKERS) and bool(_imperatives(text))


def instruction(text: str) -> bool:
    """Повелительное наклонение (кроме «давайте»), «прошу/просьба» или «нужно + глагол»."""
    low = text.lower()
    if [word for word in _imperatives(text) if word not in _CONSENT_IMPERATIVES]:
        return True
    if "прошу" in low or "просьба" in low:
        return True
    return bool(_NEED_RE.search(low) and _INFINITIVE_RE.search(low))


def has_question_word(text: str) -> bool:
    """Вопросительное слово или оборот без знака вопроса."""
    return bool(_QUESTION_WORD_RE.search(text))


def normalize_ru(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


# Прямой отзыв просьбы клиентом. «Спасибо», «понятно» и т. п. не входят: это «услышал»,
# а не «не делайте».
REQUEST_WITHDRAWN_MARKERS = (
    "не надо уже", "уже не надо", "уже не нужно", "не нужно уже",
    "отбой", "вопрос решился", "вопрос решен", "вопрос снят",
    "уже не актуально", "неактуально", "не актуально",
    "разобрались сами", "сами разобрались", "уже решили", "уже решилось",
)

# Отзыв до первой реакции распознаётся шире: ещё «вопрос решили», «уже не требуется».
WITHDRAWN_BEFORE_REACTION_MARKERS = REQUEST_WITHDRAWN_MARKERS + (
    "вопрос решил", "решили вопрос", "решил вопрос", "решила вопрос", "уже не требуется",
)


def request_withdrawn(text: str) -> bool:
    """В реплике клиента есть прямой отзыв просьбы (снимает ожидание специалиста)."""
    low = normalize_ru(text)
    return any(marker in low for marker in REQUEST_WITHDRAWN_MARKERS)


def request_withdrawn_before_reaction(text: str) -> bool:
    """В реплике клиента есть отзыв просьбы (снимает обращение до первой реакции)."""
    low = normalize_ru(text)
    return any(marker in low for marker in WITHDRAWN_BEFORE_REACTION_MARKERS)


# Метки, при которых реплика может быть отзывом просьбы. Белый список: новая метка
# не должна молча получать право снимать ожидание. `request`/`question`/`mixed_request`
# не входят — «не надо уже, но пришлите акт» остаётся новой просьбой.
WITHDRAWAL_LABELS = frozenset({
    "ack", "social", "answer", "info", "offline",
    LABEL_ADDITION, LABEL_CORRECTION,
})

# Сколько поправка или дополнение клиента считаются продолжением его речи в только что
# закрытой работе; отсчёт от последнего слова клиента в ней.
CONTINUATION_WINDOW = timedelta(minutes=10)

# Прямая просьба связаться или позвонить — по форме слова: повелительные формы 2-го лица
# целым словом и обороты ожидания звонка; изъявительные («позвонили», «созвонимся») —
# рассказ. Голый инфинитив не берётся, кроме оборота «можете/можно/прошу … <инфинитив
# звонка>» в пределах одной фразы.
_CONTACT_IMPERATIVES = (
    "позвоните", "позвони", "перезвоните", "перезвони",
    "наберите", "набери", "свяжитесь", "свяжите", "свяжись",
    "созвонитесь",
)
_CONTACT_PHRASES = (
    "жду звонка", "жду звонок", "ждем звонка", "ждем звонок",
    "нужен звонок", "нужен созвон", "давайте созвонимся", "давай созвонимся",
)
_CONTACT_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(_CONTACT_IMPERATIVES), re.I
)
# «созвонится» вместо «созвониться» — частая опечатка; после «можете/прошу» изъявительной
# формы быть не может.
_CONTACT_POLITE_RE = re.compile(
    r"\b(?:можете|можешь|можно|прошу|просьба)\b[^.?!]{0,40}?"
    r"\b(?:позвонить|перезвонить|набрать|связать?ся|созвонить?ся)\b", re.I
)


def contact_request(text: str) -> bool:
    """Прямая просьба связаться или позвонить (по форме слова)."""
    low = normalize_ru(text)
    if _CONTACT_RE.search(low) or _CONTACT_POLITE_RE.search(low):
        return True
    return any(phrase in low for phrase in _CONTACT_PHRASES)


# Факт оплаты в прошедшем времени. «не оплатил(и)», «неоплачен» не подходят
# (отрицание — lookbehind и граница слова).
_PAID_RE = re.compile(
    r"(?<!не )(?<!не\s\s)\b(?:оплатил|оплатила|оплатили|оплачено|оплачена|оплачены|оплачен|"
    r"заплатил|заплатила|заплатили|уплатил|уплатила|уплатили)\b"
    r"|\bоплат[аы]\b(?:\s+\S+){0,3}?\s+(?:прошла|проведена|произведена|внесена|поступила|отправлена)\b"
    r"|\b(?:внес|внесла|внесли|провел|провела|провели|произвел|произвела|произвели)\s+оплат[уы]\b"
    r"|\bоплат[уы]\s+(?:внес|внесла|внесли|провел|провела|провели|произвел|произвела|произвели)\b"
    r"|\b(?:платеж\w*|пп|платежк\w*|ведомост\w*)\s+подписал[аи]?\b"
    r"|\bподписал[аи]?\s+(?:платеж\w*|пп|платежк\w*|ведомост\w*)\b",
    re.I,
)
# Короткие «готово/подписала/провела…» — подтверждение, только если последняя текстовая
# реплика компании в чате просила оплатить или подписать.
_DONE_RE = re.compile(
    r"^\W*(?:все\s+|всё\s+)?(?:готово|сделано|подписал|подписала|подписали|подписано|"
    r"провел|провела|провели|оплатил|оплатила|оплатили|оплачено)\b", re.I)
_COMPANY_PAY_ASK_RE = re.compile(r"\b(?:оплат\w*|подпис\w*|пп|платежк\w*|платежн\w*|ведомост\w*)\b", re.I)
# Признаки просьбы или вопроса рядом с подтверждением оплаты — такая реплика остаётся
# обращением. Повелительное мн. числа ловит `_imperatives`.
_PAYMENT_KEEP_RE = re.compile(
    r"\b(?:прошу|просьба|пожалуйста|пожалуста|пжл|пж|пож-та|плиз|срочно|"
    r"нужн\w*|надо|необходимо|требуется|"
    r"жду|ждем|ожидаю|ожидаем|"
    r"можно|можете|могли|сможете|"
    r"дай|сделай|проверь|скинь|отправь|выстави|подготовь|посмотри|подскажи|напиши|сообщи|уточни|пришли|"
    r"чтобы|чтоб|если|когда|ли|разве|почему|зачем|как|куда|где|сколько|вроде|кажется|же|"
    r"но|однако|ошибк\w*|вернул\w*|вернули|отклон\w*|заблок\w*|задолж\w*|долг\w*|штраф\w*|пени|доплат\w*)\b",
    re.I,
)
_PAYMENT_FILE_RE = re.compile(r"(?:платеж|поручен|\bпп\b|квитанц|\bчек\b|receipt|payment)", re.I)
PURE_PAYMENT_MAX_LEN = 160


def pure_payment_confirmation(text: str, company_text: str | None = None) -> bool:
    """Текст — чистое подтверждение оплаты, без просьбы и без вопроса."""
    low = normalize_ru(text).strip()
    if not low or len(low) > PURE_PAYMENT_MAX_LEN or "?" in low:
        return False
    done = bool(_PAID_RE.search(low))
    if not done and company_text and len(low) <= 40 and _DONE_RE.search(low):
        done = bool(_COMPANY_PAY_ASK_RE.search(normalize_ru(company_text)))
    if not done:
        return False
    if _PAYMENT_KEEP_RE.search(low) or _imperatives(text) or has_question_word(text):
        return False
    return True


def payment_file(text: str, bare: bool) -> bool:
    """Вложение без подписи или с одним именем файла платёжки («Платежное_поручение.pdf»)."""
    if bare:
        return True
    low = normalize_ru(text).strip()
    return bool(low) and " " not in low and bool(_PAYMENT_FILE_RE.search(low))


# Просьба позвонить, связаться или соединить.
_CALL_RE = re.compile(r"позвон|перезвон|созвон|набер|звонк|звонок|свяжит|свяжись|соедин", re.I)

# Другая просьба в той же реплике («созвониться … пришлите прайс») — не только звонок.
_OTHER_ASK_RE = re.compile(
    r"\b(?:прислать|выслать|скинуть|отправить|направить|подготовить|сделать|проверить|посчитать|"
    r"рассчитать|оформить|выставить|подписать|перевести|оплатить)\b", re.I)


def call_only_request(text: str) -> bool:
    """Реплика — только просьба позвонить, связаться или соединить."""
    low = normalize_ru(text)
    if not _CALL_RE.search(low):
        return False
    if [word for word in _imperatives(text) if not _CALL_RE.search(word)]:
        return False
    return not _OTHER_ASK_RE.search(low)


# Просьба компании прислать материал — повелительное 2-го лица мн. числа целым словом
# либо «прошу/нужно … <инфинитив отправки>» в одной фразе. Список намеренно узкий: лишнее
# срабатывание глушит вложение клиента на сутки. «Пришли» (ед. число) — омоним прошедшего времени.
_MATERIAL_IMPERATIVES = (
    "пришлите", "перешлите", "вышлите", "отправьте", "направьте",
    "предоставьте", "приложите", "прикрепите", "загрузите",
    "скиньте", "сбросьте", "сфотографируйте", "отсканируйте",
)
_MATERIAL_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(_MATERIAL_IMPERATIVES), re.I
)
# Разговорные формы той же просьбы: «давайте сюда», «присылайте», «скидывайте», «кидайте».
_MATERIAL_INFORMAL_RE = re.compile(
    r"\bдавайте\b[^.?!]{0,20}?\bсюда\b|\b(?:присылайте|скидывайте|кидайте)\b", re.I)
# После «нужно/надо» «отправить/направить» не берутся: это обычно инструкция про третью
# сторону; после «прошу/просьба» — берутся.
_MATERIAL_POLITE_RE = re.compile(
    r"\b(?:прошу|просьба)\b[^.?!]{0,40}?"
    r"\b(?:прислать|переслать|выслать|отправить|направить|предоставить|"
    r"приложить|прикрепить|загрузить|скинуть|сбросить)\b"
    r"|\b(?:нужно|надо|необходимо|требуется)\b[^.?!]{0,40}?"
    r"\b(?:прислать|переслать|выслать|предоставить|"
    r"приложить|прикрепить|загрузить|скинуть|сбросить)\b", re.I
)

# Подпись клиента, которая при ожидании материала считается ответом. Метки просьбы
# и вопроса не входят: текстовая реплика судится как обычно.
MATERIAL_ANSWER_LABELS = frozenset({"answer", "ack", "info"})


# Просьба прислать на почту состояние «ждём материал» не взводит — материал в чат не придёт.
# «В ЛК», «в Диадок» не отсекаются: это бывает предметом просьбы, а не каналом.
_OTHER_CHANNEL_RE = re.compile(
    r"\bна\s+(?:электронн\w+\s+)?почт\w*|\bпо\s+(?:электронн\w+\s+)?почте\b|"
    r"\bна\s+e-?mail\b|\bна\s+мейл\w*|\bна\s+имейл\w*",
    re.I,
)


# Исключение из правила про «нужно отправить»: адресат назван прямо («нам», «в чат»).
_MATERIAL_TO_US_RE = re.compile(
    r"\b(?:нужно|надо|необходимо|требуется)\b[^.?!]{0,40}?"
    r"\b(?:отправить|направить)\b\s+(?:нам|мне|сюда|в\s+(?:этот\s+)?чат)\b", re.I
)


def material_request(text: str) -> bool:
    """В реплике компании есть просьба прислать материал В ЧАТ."""
    low = normalize_ru(text)
    if _MATERIAL_INFORMAL_RE.search(low):
        return True
    for pattern in (_MATERIAL_RE, _MATERIAL_POLITE_RE, _MATERIAL_TO_US_RE):
        for match in pattern.finditer(low):
            # Другой канал ищется только в той же фразе после просьбы, а не во всём сообщении.
            tail = re.split(r"[.?!\n]", low[match.start():], maxsplit=1)[0]
            if not _OTHER_CHANNEL_RE.search(tail):
                return True
    return False


# Реплика сотрудника не о работе клиента: первой реакции не даёт и ничего не закрывает.
LABEL_OTHER = "other"

# Метка `ack`; движок ставит её чистому подтверждению оплаты и платёжному файлу.
LABEL_ACK = "ack"


def is_greeting_only(message) -> bool:
    """Одно приветствие первой реакцией не считается (R-19)."""
    if message.has_media or message.media_kind:
        return False
    body = " ".join(strip_integrator_prefix(message.text).lower().split())
    return body.strip(" .,!;:") in {
        "добрый день", "доброе утро", "добрый вечер", "здравствуйте", "привет",
    }

# «Передано специалисту» — единственное, что открывает второй слой SLA.
LABEL_HANDOFF = "handoff"

# v2: вложение без текста не позже 10 минут после ответа клиента на вопрос компании —
# часть того же ответа.
ANSWER_ATTACHMENT_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class EpisodeMessage:
    """Вход движка: отсоединённый и неизменяемый, к ORM-сессии не привязан."""

    id: int
    chat_id: int
    thread_id: int | None
    sent_at: datetime
    business_side: BusinessSide
    text: str | None
    has_media: bool
    media_kind: str | None
    transport_actor_kind: TransportActorKind
    reply_to_tg_message_id: int | None = None
    # `tg_message_id` разворачивает reply во внутренний id, `tg_user_id`
    # отличает поправку к своей работе от поправки соседа.
    tg_message_id: int | None = None
    tg_user_id: int | None = None


@dataclass(frozen=True)
class EpisodeReplayInput:
    """Снимок пачки только для чтения. JSON настроек хранит историю версий.

    Сообщения уже прошли исторический фильтр наблюдения. Строки рассылок
    включают другие чаты, но каждый префикс фильтрует их до распознавания.
    В словарях — скаляры или кортежи, изменяемых ORM-объектов нет.
    """

    chat_ids: tuple[int, ...]
    messages_by_chat: Mapping[int, tuple[EpisodeMessage, ...]]
    forum_map: Mapping[int, bool]
    attribution_map: Mapping[int, int]
    not_staff_messages: frozenset[int]
    verdicts: Mapping[int, tuple]
    broadcast_rows: tuple[tuple[int, int, datetime, str | None], ...]
    calendar_json: str
    episodes_json: str
    alerts_json: str
    rules_since: tuple[datetime | None, datetime | None, datetime | None]
    ai_shadow_mode: bool
    before: tuple[datetime, int] | None = None


def _before_message(boundary: tuple[datetime, int]):
    moment, message_id = boundary
    return or_(Message.sent_at < moment, and_(Message.sent_at == moment, Message.id < message_id))


async def prepare_episode_replay(
    session: AsyncSession,
    *,
    chat_ids: set[int],
    before: tuple[datetime, int] | None = None,
) -> EpisodeReplayInput:
    """Прочитать выбранные чаты один раз, без flush и без изменения объектов сессии.

    `before` — последняя граница пачки (не включительно). Префикс позже неё
    из этого снимка не переигрывается. Подмену вердиктов передаёт вызывающий,
    отдельно для каждой переигровки.
    """
    from app.config import get_settings

    settings = get_settings()
    with session.no_autoflush:
        sections = [await get_section(session, key) for key in ("work_calendar", "episodes", "alerts")]
        tracked = tuple((await session.scalars(
            select(ChatTrackingPeriod.chat_id).distinct().where(ChatTrackingPeriod.chat_id.in_(chat_ids))
        )).all())
        query = select(Message).where(
            Message.chat_id.in_(tracked),
            Message.business_side.in_([BusinessSide.CLIENT, BusinessSide.COMPANY]),
            observed_filter(),
        )
        if before is not None:
            query = query.where(_before_message(before))
        rows = (await session.scalars(query.order_by(Message.sent_at, Message.id))).all()
        detached = {chat_id: [] for chat_id in tracked}
        for row in rows:
            detached[row.chat_id].append(EpisodeMessage(
                row.id, row.chat_id, row.thread_id, row.sent_at, row.business_side,
                row.text, row.has_media, row.media_kind, row.transport_actor_kind,
                row.reply_to_tg_message_id, row.tg_message_id, row.tg_user_id,
            ))
        message_ids = [row.id for row in rows]
        attribution = (await session.execute(select(Attribution.message_id, Attribution.staff_id).where(
            Attribution.message_id.in_(message_ids), Attribution.staff_id.isnot(None)
        ))).all()
        not_staff = (await session.scalars(select(Attribution.message_id).where(
            Attribution.message_id.in_(message_ids), not_staff_condition()
        ))).all()
        classifications = select(
            Classification.message_id, Classification.requires_response,
            Classification.is_substantive, Classification.label, Classification.answers_request_id,
        ).where(
            Classification.message_id.in_(message_ids),
            Classification.model.in_(settings.ai_accepted_models), Classification.error.is_(None),
            # Технический вердикт («спросить не вышло») — не разметка: сообщение должно
            # остаться неразмеченным, а не получить вердикт «ничего не требуется».
            Classification.source != SOURCE_TECHNICAL,
        ).order_by(Classification.message_id, Classification.prompt_version.asc(), Classification.id.asc())
        if settings.ai_shadow_mode:
            classifications = classifications.where(Classification.source == SOURCE_RULE)
        cls_rows = (await session.execute(classifications)).all()
        forums = (await session.execute(select(Chat.id, Chat.is_forum).where(Chat.id.in_(tracked)))).all()
        broadcast_query = select(Message.id, Message.chat_id, Message.sent_at, Message.text).where(
            Message.business_side == BusinessSide.COMPANY, Message.text.isnot(None)
        )
        if before is not None:
            broadcast_query = broadcast_query.where(_before_message(before))
        broadcast_rows = (await session.execute(broadcast_query)).all()
    return EpisodeReplayInput(
        tracked, MappingProxyType({key: tuple(value) for key, value in detached.items()}),
        MappingProxyType({row.id: bool(row.is_forum) for row in forums}),
        MappingProxyType({row.message_id: row.staff_id for row in attribution}), frozenset(not_staff),
        MappingProxyType({row.message_id: tuple(row)[1:] for row in cls_rows}),
        tuple(tuple(row) for row in broadcast_rows),
        *(json.dumps(section, default=lambda value: value.isoformat()) for section in sections),
        (settings.episode_rules_v2_since, settings.episode_rules_v3_since, settings.episode_rules_v4_since),
        settings.ai_shadow_mode, before,
    )


def handoff_deadline(
    handoff_at: datetime | None, seconds: int, calendar_cfg: dict
) -> datetime | None:
    """Срок ответа специалиста: то же время следующего рабочего дня; None — передачи не было.

    Общая для движка и алертов: правило обязано быть одним.
    """
    if handoff_at is None:
        return None
    return same_time_next_workday(handoff_at, seconds, calendar_cfg)


def _handoff_breached(
    handoff_at: datetime | None,
    answered_at: datetime,
    seconds: int,
    calendar_cfg: dict,
) -> bool | None:
    """Нарушен ли срок специалиста. None — передачи не было, слоя нет."""
    deadline = handoff_deadline(handoff_at, seconds, calendar_cfg)
    if deadline is None:
        return None
    return answered_at > deadline


def alert_thresholds(alert_cfg: dict) -> tuple[int, int]:
    """Пороги в секундах: первая реакция, ответ специалиста после передачи."""
    return (
        int(alert_cfg.get("threshold_minutes") or DEFAULT_REACTION_MINUTES) * 60,
        int(alert_cfg.get("substantive_threshold_minutes") or DEFAULT_SPECIALIST_MINUTES) * 60,
    )


def wait_windows(episode_cfg: dict) -> tuple[int, int]:
    """Окна ожидания в секундах: после срока реакции и после срока специалиста."""
    return (
        int(episode_cfg.get("wait_reaction_hours") or DEFAULT_WAIT_REACTION_HOURS) * 3600,
        int(episode_cfg.get("wait_specialist_days") or DEFAULT_WAIT_SPECIALIST_DAYS) * 86400,
    )


def deadline_moment(
    interaction: Interaction,
    calendar_cfg: dict,
    *,
    sla_seconds: int,
    substantive_seconds: int,
) -> datetime:
    """Последний наступающий срок обращения.

    Важно: окно ожидания отсчитывается от последнего срока, а не от сообщения клиента: иначе
    обращение закрывается раньше, чем алерт получит право сработать, при любых настройках.
    """
    moments = [interaction.opened_at]

    # Первый слой: реакции ещё не было — её срок впереди или только что.
    if interaction.first_reaction_at is None:
        first = response_deadline(interaction.opened_at, sla_seconds, calendar_cfg)
        if first is not None:
            moments.append(first)

    # Второй слой: обещали специалиста — у него свой срок.
    second = handoff_deadline(
        interaction.handoff_at, substantive_seconds, calendar_cfg
    )
    if second is not None:
        moments.append(second)

    return max(moments)


def expiry_moment(
    interaction: Interaction,
    calendar_cfg: dict,
    *,
    sla_seconds: int,
    substantive_seconds: int,
    wait_reaction_seconds: int,
    wait_specialist_seconds: int,
) -> datetime:
    """Момент, после которого ждать перестаём.

    Окно календарное и отсчитывается от последнего наступающего срока (`deadline_moment`),
    а не от сообщения клиента. Функция общая для движка и сводки алертов.
    """
    window = (
        wait_specialist_seconds
        if interaction.handoff_at is not None
        else wait_reaction_seconds
    )
    deadline = deadline_moment(
        interaction,
        calendar_cfg,
        sla_seconds=sla_seconds,
        substantive_seconds=substantive_seconds,
    )
    # Важно: окно начинается не раньше, чем компания снова работает: срок, упавший на нерабочее
    # время, иначе сгорал бы раньше, чем алерт (он шлётся только в рабочее время) мог уйти.
    start = next_working_moment(deadline, calendar_cfg) or deadline
    return start + timedelta(seconds=window)


async def rebuild_interactions(
    session: AsyncSession | None,
    *,
    now: datetime | None = None,
    verdict_override: dict[int, tuple[bool | None, bool | None, str | None]] | None = None,
    persist: bool = True,
    replay_input: EpisodeReplayInput | None = None,
    chat_ids: set[int] | None = None,
    before: tuple[datetime, int] | None = None,
    settle_open: bool = True,
) -> dict:
    """Полная пересборка эпизодов по всем отслеживаемым чатам.

    `now` — момент, относительно которого стареют открытые эпизоды (для тестов).
    `verdict_override` — `message_id -> (requires_response, is_substantive, label[, link])`
    поверх вердиктов из базы; при `persist=False` таблица эпизодов не трогается,
    собранные эпизоды возвращаются ключом `items`.

    `replay_input` — готовый отсоединённый снимок пачки, база не читается.
    `chat_ids` ограничивает только переигровку без записи. `before=(sent_at, id)`
    отсекает текущее сообщение и всё позже, включая рассылки в других чатах;
    `now` должен совпадать со временем этой границы. `settle_open=False` оставляет
    открытые обращения как есть — для контекста классификации, — но истёкшие
    к границе ожидания закрывает. Неистёкшие подтверждения помощника остаются
    доступны для присоединения. Закрытия по сообщениям и версии правил действуют.
    """
    from app.config import get_settings

    selected_chat_ids = chat_ids
    if replay_input is not None or before is not None or selected_chat_ids is not None or not settle_open:
        if persist:
            raise ValueError("Частичный или префиксный прогон требует persist=False")
    if replay_input is not None and replay_input.before is not None and before is None:
        raise ValueError("Снимку с границей нужна явная граница префикса")
    if before is not None:
        if now is None or now != before[0]:
            raise ValueError("Префиксному прогону нужен now, равный времени исключающей границы")
        if replay_input is not None and replay_input.before is not None and before > replay_input.before:
            raise ValueError("Граница префикса позже подготовленного снимка")
    if replay_input is None and (before is not None or selected_chat_ids is not None):
        if session is None:
            raise ValueError("Для подготовки прогона нужна сессия")
        if selected_chat_ids is None:
            raise ValueError("Префиксному прогону нужен явный список чатов")
        replay_input = await prepare_episode_replay(session, chat_ids=selected_chat_ids, before=before)
    if replay_input is None and session is None:
        raise ValueError("Обычной пересборке нужна сессия")

    settings = get_settings()
    rules_v2_since = settings.episode_rules_v2_since
    rules_v3_since = settings.episode_rules_v3_since
    rules_v4_since = settings.episode_rules_v4_since
    if replay_input is not None:
        rules_v2_since, rules_v3_since, rules_v4_since = replay_input.rules_since

    def engine_version_at(moment: datetime) -> int:
        if rules_v4_since is not None and moment >= rules_v4_since:
            return 4
        if rules_v3_since is not None and moment >= rules_v3_since:
            return 3
        return 2 if rules_v2_since is not None and moment >= rules_v2_since else 1

    calendar_cfg = (await get_section(session, "work_calendar") if replay_input is None
                    else json.loads(replay_input.calendar_json))
    # Важно: график берётся на момент обращения, а не текущий: смена графика не переписывает
    # просрочки прошлого. Один календарь на всё обращение — иначе алерт и отчёт разойдутся.
    calendar = CalendarHistory(calendar_cfg)
    episode_cfg = (await get_section(session, "episodes") if replay_input is None
                   else json.loads(replay_input.episodes_json))
    alert_cfg = (await get_section(session, "alerts") if replay_input is None
                 else json.loads(replay_input.alerts_json))

    now = now or datetime.now(timezone.utc)
    max_messages = int(episode_cfg.get("max_messages") or 50)

    # Важно: пороги и окна — тоже на момент обращения: смена порога не переписывает историю.
    alerts_history = History(alert_cfg, versioned_fields("alerts"))
    episodes_history = History(episode_cfg, versioned_fields("episodes"))

    def rules_at(moment: datetime) -> tuple[int, int, int, int]:
        """(порог реакции, срок специалиста, окно реакции, окно специалиста)."""
        sla, substantive = alert_thresholds(alerts_history.at(moment))
        reaction, specialist = wait_windows(episodes_history.at(moment))
        return sla, substantive, reaction, specialist

    def credit_specialist_contact(item: Interaction, message, staff_id) -> None:
        """Засчитать ожиданию специалиста выход на связь этой репликой (как `answer_target`)."""
        if item.substantive_at is not None:
            return
        case_cfg = calendar.at(item.opened_at)
        _, case_substantive, _, _ = rules_at(item.opened_at)
        item.substantive_at = message.sent_at
        item.substantive_message_id = message.id
        item.substantive_staff_id = staff_id
        item.ttfa_seconds = int((message.sent_at - item.opened_at).total_seconds())
        item.ttfa_business_seconds = business_seconds(item.opened_at, message.sent_at, case_cfg)
        item.substantive_breached = _handoff_breached(
            item.handoff_at, message.sent_at, case_substantive, case_cfg
        )

    def settle(interaction: Interaction, client_ids: list[int], *, expired: bool) -> str | None:
        """Каким состоянием закрыть обращение, за которым больше некого ждать; None — ждём дальше.

        Одно правило для разреза в цикле и финального прохода; ABANDONED — только при `expired`.
        """
        flags = [requires_response(message_id) for message_id in client_ids]
        if flags and all(flag is False for flag in flags):
            # Ни одно сообщение клиента ответа не требовало.
            interaction.state = InteractionState.NO_RESPONSE_NEEDED
            return "no_response"

        if interaction.first_reaction_at is not None and interaction.handoff_at is None:
            # Реакция была, передачи не было — помощник закрыл вопрос сам.
            interaction.state = InteractionState.ANSWERED
            return "by_assistant"

        if interaction.substantive_at is not None:
            # Специалист вышел на связь встречным вопросом, клиент замолчал: ход был за клиентом.
            interaction.state = InteractionState.ANSWERED
            return "by_assistant"

        if expired:
            interaction.state = InteractionState.ABANDONED
            return "abandoned"
        return None

    def expired_at(interaction: Interaction) -> datetime:
        """Момент, после которого ждать перестаём (см. `expiry_moment`)."""
        sla, substantive, reaction, specialist = rules_at(interaction.opened_at)
        return expiry_moment(
            interaction,
            calendar.at(interaction.opened_at),
            sla_seconds=sla,
            substantive_seconds=substantive,
            wait_reaction_seconds=reaction,
            wait_specialist_seconds=specialist,
        )

    # Чаты, которые наблюдались хоть когда-то: эпизоды прошлого не исчезают из-за паузы;
    # сами сообщения дальше фильтруются по интервалам наблюдения.
    chat_ids = ((await session.scalars(select(ChatTrackingPeriod.chat_id).distinct())).all()
                if replay_input is None else [chat_id for chat_id in replay_input.chat_ids
                if selected_chat_ids is None or chat_id in selected_chat_ids])

    if persist:
        await session.execute(delete(Interaction))

    if replay_input is None:
        # Атрибуция сообщений компании: message_id -> staff_id.
        attribution_map = {
            row.message_id: row.staff_id
            for row in (
                await session.execute(
                    select(Attribution.message_id, Attribution.staff_id).where(
                        Attribution.staff_id.isnot(None)
                    )
                )
            ).all()
        }

        # Подписи, помеченные «это не сотрудник» (рассылки интегратора, боты): реакцией
        # и ответом они быть не могут.
        not_staff_messages = {
            row.message_id
            for row in (
                await session.execute(
                    select(Attribution.message_id).where(not_staff_condition())
                )
            ).all()
        }

        # Рассылки (v2): одинаковое тело длиннее 60 символов в 3+ чатах за 10 минут — не реакция.
        # Считаются по всем сообщениям компании, включая чаты на паузе.
        broadcast_ids: set[int] = set()
        if (
            rules_v2_since is not None
            or rules_v3_since is not None
            or rules_v4_since is not None
        ):
            broadcast_ids = broadcast_message_ids(
                (row.id, row.chat_id, row.sent_at, row.text)
                for row in (
                    await session.execute(
                        select(Message.id, Message.chat_id, Message.sent_at, Message.text)
                        .where(Message.business_side == BusinessSide.COMPANY)
                        .where(Message.text.isnot(None))
                    )
                ).all()
            )

        # Классификации: message_id -> (requires_response, is_substantive). В тихом режиме
        # (AI_SHADOW_MODE) на эпизоды влияют только дешёвые правила, вердикты модели — нет.
        verdict_query = (
            select(
                Classification.message_id,
                Classification.requires_response,
                Classification.is_substantive,
                Classification.label,
                # Связь ответа с просьбой; у старых вердиктов NULL — v4 работает без связи.
                Classification.answers_request_id,
            )
            # Вердикты текущей и предыдущих моделей (`AI_PREVIOUS_MODELS`) действительны;
            # на одном сообщении побеждает старшая версия промпта.
            .where(Classification.model.in_(settings.ai_accepted_models))
            .where(Classification.error.is_(None))
            # Технический вердикт движок не читает: сообщение остаётся неразмеченным.
            .where(Classification.source != SOURCE_TECHNICAL)
            # Порядок обязателен: у сообщения бывают вердикты нескольких версий промпта, и перезапись
            # в dict ниже оставляет последнюю (при равных версиях — более позднюю строку). Фильтр
            # «только текущая версия» не годится: на время переклассификации эпизоды ослепли бы.
            .order_by(
                Classification.message_id,
                Classification.prompt_version.asc(),
                Classification.id.asc(),
            )
        )
        if settings.ai_shadow_mode:
            verdict_query = verdict_query.where(Classification.source == SOURCE_RULE)
        cls_rows = (await session.execute(verdict_query)).all()
        verdicts = {row.message_id: (row.requires_response, row.is_substantive) for row in cls_rows}
        labels = {row.message_id: row.label for row in cls_rows}
        links = {row.message_id: row.answers_request_id for row in cls_rows}
    else:
        attribution_map = replay_input.attribution_map
        not_staff_messages = replay_input.not_staff_messages
        broadcast_ids = broadcast_message_ids(
            row for row in replay_input.broadcast_rows
            if before is None or (row[2], row[0]) < before
        )
        # Фильтр хвоста берёт эти id даже при выключенных границах правил;
        # реакции движка смотрят на них только под условием v2.
        verdicts = {mid: value[:2] for mid, value in replay_input.verdicts.items()}
        labels = {mid: value[2] for mid, value in replay_input.verdicts.items()}
        links = {mid: value[3] for mid, value in replay_input.verdicts.items()}
    # Подмена вердиктов поверх базы, той же формы; четвёртый элемент (связь ответа) необязателен.
    for message_id, verdict in (verdict_override or {}).items():
        requires, substantive, label = verdict[0], verdict[1], verdict[2]
        verdicts[message_id] = (requires, substantive)
        labels[message_id] = label
        if len(verdict) > 3:
            links[message_id] = verdict[3]

    # Сообщения клиента, признанные ответом на вопрос или просьбу компании (v2), и вложения
    # к такому ответу: ответа компании они не требуют, что бы ни решила модель.
    answer_ids: set[int] = set()

    # v4: `addition`/`correction` без открытого обращения — новая просьба. Множество нужно,
    # потому что состав обращения перечитывается позже (`settle`, `had_question`).
    promoted_requests: set[int] = set()

    def requires_response(message_id: int) -> bool | None:
        """True/False — вердикт есть; None — сообщение ещё не классифицировано.

        Приветствия и благодарности не открывают SLA-таймер, даже если модель
        сочла, что «на приветствие принято отвечать»: label сильнее флага.
        """
        if message_id in answer_ids:
            return False
        if message_id in promoted_requests:
            return True
        verdict = verdicts.get(message_id)
        if verdict is None:
            return None
        requires, _ = verdict
        label = labels.get(message_id)
        if label in ("social", "ack", "offline", "answer", "addition", "correction"):
            # offline — ответ уйдёт вне чата; answer/addition/correction — ответ нам, дополнение
            # или поправка открытой просьбы: нового дела ни одна не создаёт.
            return False
        return requires

    # Темы (thread_id) учитываются только в форум-группах: в обычной группе Telegram ставит
    # message_thread_id любому reply, и связка вопрос-ответ рвалась бы.
    if replay_input is None:
        forum_map = {
            row.id: bool(row.is_forum)
            for row in (await session.execute(select(Chat.id, Chat.is_forum))).all()
        }
    else:
        forum_map = replay_input.forum_map

    built = 0
    open_tracker: list[tuple[Interaction, list[int]]] = []
    # Счётчики закрытий: общие для разреза в цикле и финального прохода —
    # решение принимает один `settle`, значит и считает он же.
    closed = {"no_response": 0, "by_assistant": 0, "abandoned": 0}
    for chat_id in chat_ids:
        use_threads = forum_map.get(chat_id, False)
        thread_order = (
            (Message.thread_id.nulls_first(), Message.sent_at, Message.id)
            if use_threads
            else (Message.sent_at, Message.id)
        )
        if replay_input is None:
            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.chat_id == chat_id)
                    .where(Message.business_side.in_([BusinessSide.CLIENT, BusinessSide.COMPANY]))
                    .where(observed_filter())
                    .order_by(*thread_order)
                )
            ).all()
        else:
            messages = [message for message in replay_input.messages_by_chat.get(chat_id, ())
                        if before is None or (message.sent_at, message.id) < before]
            messages.sort(key=lambda message: (
                (message.thread_id is not None, message.thread_id or 0, message.sent_at, message.id)
                if use_threads else (message.sent_at, message.id)
            ))

        # Reply приходит номером транспорта; карта строится по тем же сообщениям,
        # что и разбор, поэтому reply «в будущее» не разрешится.
        tg_to_id = {
            message.tg_message_id: message.id
            for message in messages
            if message.tg_message_id is not None
        }
        # Автор каждого сообщения клиента: по нему отличается поправка
        # к своей работе от реплики соседа по чату.
        author_of = {message.id: message.tg_user_id for message in messages}

        current: Interaction | None = None
        current_ids: list[int] = []
        current_thread: int | None = None
        # Последняя текстовая реплика компании в чате: по ней понимается короткое «готово» клиента.
        last_company_text: str | None = None
        # Тексты и «голые» вложения чата — для подтверждений оплаты и просьб о звонке; последнее
        # чистое подтверждение оплаты (автор, момент) — для платёжного файла вслед за ним.
        texts = {m.id: strip_integrator_prefix(m.text) or "" for m in messages}
        bare_files = {
            m.id: bool(m.has_media and m.media_kind in SUBSTANTIVE_MEDIA
                       and not strip_integrator_prefix(m.text))
            for m in messages
        }
        last_payment: tuple[int | None, datetime] | None = None

        def call_only_work(ids: list[int]) -> bool:
            """Все просьбы обращения — позвонить, связаться или соединить (`offline` или по тексту)."""
            asks = [mid for mid in ids if labels.get(mid) == "offline" or requires_response(mid) is True]
            return bool(asks) and all(
                labels.get(mid) == "offline" or call_only_request(texts.get(mid, "")) for mid in asks
            )
        # Момент встречного вопроса компании: ход за клиентом, его реплика — ответ, а не просьба.
        # С v2 это состояние чата (действует и при закрытом обращении) до того же времени
        # следующего рабочего дня.
        awaiting_at: datetime | None = None
        # Момент просьбы компании прислать материал. Отдельно от `awaiting_at`: окно ответа
        # глушит любую реплику клиента, а это состояние — только вложение без текста и подпись к нему.
        material_at: datetime | None = None
        # Начал ли клиент присылать материал; пока нет, реплики компании состояние не гасят.
        material_answered = False
        # Момент последнего сообщения клиента, признанного ответом (v2): к нему
        # ещё `ANSWER_ATTACHMENT_WINDOW` могут присоединяться вложения без текста.
        last_answer_at: datetime | None = None
        # v3: обращения, переданные специалисту и отложенные ради новой
        # просьбы клиента, по открывающему сообщению; порядок — порядок
        # парковки, возобновляется последнее.
        parked: dict[int, tuple[Interaction, list[int]]] = {}
        # v4: все открытые обращения чата в порядке открытия (ждущие реакции и ждущие специалиста).
        # В v1–v3 список пуст; `current` в v4 — его последний элемент.
        open_v4: list[tuple[Interaction, list[int]]] = []
        # Пачка вложений, явно начатая дополнением или поправкой клиента. Ведётся
        # по каждой работе, кончается её подтверждением или паузой в десять минут.
        update_material_at: dict[int, datetime] = {}
        # Закрытые работы — состав, момент последнего слова клиента и признак «закрылась ответом
        # по существу». Наполняется в `v4_drop`.
        closed_works: list[tuple[list[int], datetime, bool]] = []
        # Сообщение закрытой работы -> её первая реакция («пачка»): ссылка модели указывает
        # на сообщение, а работы в `open_v4` уже нет. Наполняется в `v4_drop`.
        closed_batch_anchor: dict[int, int] = {}
        sent_of = {message.id: message.sent_at for message in messages}

        def close_current() -> None:
            nonlocal current, current_ids, awaiting_at
            if current is not None and current.version < 2:
                # v1: встречный вопрос жил только внутри обращения.
                awaiting_at = None
            # Обращение v4, закрытое веткой v1–v3, убирается из `open_v4`, иначе `v4_sync`
            # снова сделал бы закрытую работу текущей.
            if current is not None and current.version >= 4:
                v4_drop(current)
            current = None
            current_ids = []

        def v4_enabled(moment: datetime) -> bool:
            """Работают ли правила v4 на этой реплике.

            Пока открыто (или припарковано) обращение младшей версии, чат идёт старой веткой:
            граница версий не переписывает прошлое.
            """
            if engine_version_at(moment) < 4:
                return False
            if current is not None and current.version < 4:
                return False
            return all(item.version >= 4 for item, _ in parked.values())

        def v4_sync() -> None:
            """`current` в v4 — последнее открытое обращение чата.

            Важно: пустой `open_v4` не затирает живое обращение старой версии: его ведёт ветка v1–v3.
            """
            nonlocal current, current_ids
            if open_v4:
                current, current_ids = open_v4[-1]
            elif current is None or current.version >= 4:
                current = None
                current_ids = []

        def v4_settle_finished() -> None:
            """Закрыть обращения с реакцией без передачи — ждать по ним больше некого.

            Важно: иначе обращение, ставшее не последним, ловило бы чужую передачу и зависало
            ожиданием специалиста. Последнее открытое остаётся: к нему присоединяются реплики.
            """
            for item, ids in list(open_v4[:-1]):
                if item.first_reaction_at is not None and item.handoff_at is None:
                    item.state = InteractionState.ANSWERED
                    closed["by_assistant"] += 1
                    v4_drop(item)

        def v4_drop(target: Interaction) -> None:
            """Убрать обращение из открытых; сравнение по тождеству — составы бывают равны по `==`."""
            for index, (item, ids) in enumerate(open_v4):
                if item is target:
                    # Состав закрытой работы нужен, чтобы узнать, чьи это были слова.
                    if ids:
                        closed_works.append((
                            list(ids),
                            max(sent_of[mid] for mid in ids if mid in sent_of),
                            target.substantive_at is not None,
                        ))
                    # Запомнить пачку закрытой работы (`closed_batch_anchor`).
                    if target.first_reaction_message_id is not None:
                        for mid in ids:
                            closed_batch_anchor[mid] = target.first_reaction_message_id
                    del open_v4[index]
                    return

        def own_client_work(message, ids: list[int]) -> bool:
            """Писал ли этот человек хоть одно слово в этой работе.

            Неизвестный автор (у реплики или у всей работы) — работа считается своей.
            """
            author = author_of.get(message.id)
            if author is None:
                return True
            authors = {author_of.get(mid) for mid in ids}
            return author in authors or authors <= {None}

        def continues_own_speech(message) -> bool:
            """Клиент правит или дополняет свои слова из работы, закрытой не позже
            `CONTINUATION_WINDOW` назад и не ответом по существу (признак «своей» — `own_client_work`).
            """
            for ids, last_at, answered in reversed(closed_works):
                if message.sent_at - last_at > CONTINUATION_WINDOW:
                    continue
                if answered:
                    continue
                if own_client_work(message, ids):
                    return True
            return False

        for message in messages:
            thread = message.thread_id if use_threads else None
            v4_mode = v4_enabled(message.sent_at)
            if v4_mode:
                if use_threads:
                    # Ветка форума сменилась — обращения прошлой темы больше
                    # не принимают реплик; чем они закончились, решит
                    # финальный проход, как и `close_current` в v1–v3.
                    for item, ids in [pair for pair in open_v4 if pair[0].thread_id != thread]:
                        v4_drop(item)
                v4_sync()
                current_thread = thread
            elif current is None and parked:
                # v3: текущего обращения нет — возвращаем последнее
                # припаркованное ожидание специалиста: реплика компании
                # станет его ответом, просьба клиента снова его отложит.
                available = [
                    key for key, (item, _) in parked.items()
                    if not use_threads or item.thread_id == thread
                ]
                if available:
                    current, current_ids = parked.pop(available[-1])
                    current_thread = current.thread_id if use_threads else None
            if current is not None and thread != current_thread:
                close_current()
            current_thread = thread

            if message.business_side is BusinessSide.CLIENT:
                # Правила v2 для слова клиента: по моменту сообщения, но
                # внутри обращения, открытого до границы, остаётся v1.
                v2_message = engine_version_at(message.sent_at) >= 2 and (
                    current is None or current.version >= 2
                )
                # v1–v3: ответом на встречный вопрос считается только эта реплика — флаг гаснет до разбора.
                # v4: ход у клиента держится до срока окна; его снимает истечение срока, следующая
                # содержательная реплика компании или собственная просьба клиента, вскрывшая окно.
                if v2_message:
                    # v2: ход у клиента до того же времени следующего рабочего дня после вопроса
                    # компании; позже давний вопрос не глотает новую просьбу.
                    answers_counter_question = False
                    if awaiting_at is not None:
                        answer_until = same_time_next_workday(
                            awaiting_at, 86400, calendar.at(awaiting_at)
                        )
                        answers_counter_question = (
                            answer_until is None or message.sent_at <= answer_until
                        )
                else:
                    answers_counter_question = awaiting_at is not None
                if not (v4_mode and v2_message and answers_counter_question):
                    # Всё, что уже вне окна, — как в v1–v3: ожидание ответа клиента снимается.
                    awaiting_at = None
                # «Без текста» — после снятия префикса интегратора: у клиентов
                # «(К)» вложение приходит с текстом «… делится файлом».
                bare_attachment = (
                    message.has_media
                    and message.media_kind in SUBSTANTIVE_MEDIA
                    and not strip_integrator_prefix(message.text)
                )
                # Окно ожидания (v4): внутри него метки `request`/`question` обращения не открывают —
                # это ответ клиента (R-22); окно вскрывают вопросительный знак, просьба о звонке
                # и `mixed_request`. Вне окна (в том числе после передачи специалисту, где
                # `awaiting_at` не взводится) метки действуют обычно. Файл без подписи при отсутствии
                # открытых работ — неучтённый документ, реакция на него нужна и в окне.
                client_text = strip_integrator_prefix(message.text) or ""
                # Чистое подтверждение оплаты с меткой `request` — не обращение: метка читается как `ack`.
                # «Оплатили, пришлите закрывающие», «оплатили?» остаются обращениями.
                if (
                    v4_mode
                    and labels.get(message.id) == "request"
                    and pure_payment_confirmation(client_text, last_company_text)
                ):
                    labels[message.id] = LABEL_ACK
                    last_payment = (author_of.get(message.id), message.sent_at)
                    # Платёжный файл того же автора перед подтверждением: обращение только из таких
                    # файлов, открыто не раньше `ANSWER_ATTACHMENT_WINDOW` назад, без реакции, —
                    # часть подтверждения.
                    if open_v4:
                        pay_item, pay_ids = open_v4[-1]
                        if (
                            pay_item.first_reaction_at is None
                            and pay_item.handoff_at is None
                            and message.sent_at - pay_item.opened_at <= ANSWER_ATTACHMENT_WINDOW
                            and all(
                                author_of.get(mid) == author_of.get(message.id)
                                and payment_file(texts.get(mid, ""), bare_files.get(mid, False))
                                for mid in pay_ids
                            )
                        ):
                            for mid in pay_ids:
                                if bare_files.get(mid):
                                    answer_ids.add(mid)
                                else:
                                    labels[mid] = LABEL_ACK
                elif (
                    v4_mode
                    and last_payment is not None
                    and author_of.get(message.id) == last_payment[0]
                    and message.sent_at - last_payment[1] <= ANSWER_ATTACHMENT_WINDOW
                    and payment_file(client_text, bare_attachment)
                ):
                    # Платёжный файл вслед за чистым подтверждением оплаты — то же подтверждение.
                    if bare_attachment:
                        answer_ids.add(message.id)
                    else:
                        labels[message.id] = LABEL_ACK
                # Стебли просьбы позвонить или связаться вскрывают окно.
                window_markers = CALL_REQUEST_MARKERS + CONTACT_REQUEST_MARKERS
                answer_window_opens_work = "?" in client_text or any(
                    marker in client_text.lower() for marker in window_markers
                )
                # Метка `offline` с прямой просьбой связаться (по форме слова) — просьба со своим
                # сроком (`promoted_requests`).
                offline_contact = (
                    v4_mode
                    and labels.get(message.id) == "offline"
                    and contact_request(client_text)
                )
                if offline_contact:
                    promoted_requests.add(message.id)
                # Чем ещё реплика клиента вскрывает окно — только там, где модель увидела просьбу
                # или вопрос (requires_response), а окно ожидания его перекрыло: поручение
                # и, при метке `question`, вопросительное слово без «?».
                window_opened_by_text = False
                if v4_mode and answers_counter_question and not answer_window_opens_work:
                    window_label = labels.get(message.id)
                    if window_label in (
                        "request", "question", LABEL_MIXED_REQUEST,
                    ) and requires_response(message.id) is True:
                        if polite_instruction(client_text) or instruction(client_text):
                            window_opened_by_text = True
                        if window_label == "question" and has_question_word(client_text):
                            window_opened_by_text = True
                # Просьба открывает своё обращение (R-18), если единственное открытое
                # обращение не требует ответа, без реакции и передачи, и его срок реакции уже
                # истёк. Нужны оба условия: без первого просьбы внутри окна стали бы
                # обращениями, без второго рвалась бы живая беседа. `addition`/`correction`
                # дополняют свою работу.
                if current is None:
                    resume_host_gone = False
                else:
                    host_sla, host_substantive, _, _ = rules_at(current.opened_at)
                    resume_host_gone = (
                        current.first_reaction_at is None
                        and current.substantive_at is None
                        and current.handoff_at is None
                        and bool(current_ids)
                        and all(
                            requires_response(mid) is False for mid in current_ids
                        )
                        and message.sent_at > deadline_moment(
                            current,
                            calendar.at(current.opened_at),
                            sla_seconds=host_sla,
                            substantive_seconds=host_substantive,
                        )
                    )
                if v4_mode and (
                    labels.get(message.id) in (
                        "request", "question", LABEL_MIXED_REQUEST,
                    ) and requires_response(message.id) is True
                    # `offline` с просьбой связаться — такая же просьба.
                    or offline_contact
                ) and (
                    labels.get(message.id) == LABEL_MIXED_REQUEST
                    or offline_contact
                    or not answers_counter_question
                    or answer_window_opens_work
                    or resume_host_gone
                    or window_opened_by_text
                    or (bare_attachment and current is None and not open_v4)
                ):
                    answers_counter_question = False
                    last_answer_at = None
                    # Реплика вскрыла окно и открывает обращение — ход
                    # снова у компании, остаток окна ей не принадлежит.
                    awaiting_at = None
                # Серия ответа жива, пока клиент без перерыва в 10 минут шлёт к нему вложения и пояснения.
                answer_series_alive = (
                    last_answer_at is not None
                    and message.sent_at - last_answer_at <= ANSWER_ATTACHMENT_WINDOW
                )
                # Пока взведено «ждём материал» (`material_at`, до того же времени следующего
                # рабочего дня), ответом считаются только вложение без текста, их серия и короткая
                # подпись `answer`/`ack`/`info`. Текстовую просьбу или вопрос состояние не глушит
                # и не гасит: клиент часто спрашивает, а запрошенное досылает следом.
                if material_at is None:
                    material_answer = False
                else:
                    material_until = same_time_next_workday(
                        material_at, 86400, calendar.at(material_at)
                    )
                    material_answer = (
                        v4_mode
                        and (material_until is None
                             or message.sent_at <= material_until)
                        and (
                            bare_attachment
                            or labels.get(message.id) in MATERIAL_ANSWER_LABELS
                        )
                    )
                # Намеренно нет правила «второе вложение вслед за отвеченным — та же пачка»: без
                # содержимого его не отличить от непрошеного файла; перекос в пользу клиента.
                if v2_message and (
                    answers_counter_question
                    # Вложение без текста вслед за ответом клиента — часть того же ответа; окно
                    # от последнего сообщения серии, а не от состава обращения.
                    or (answer_series_alive and bare_attachment)
                    # Запрошенный материал внутри своего состояния.
                    or material_answer
                ):
                    # Ответ клиента — текстом или вложением — без таймера, даже при метке просьбы
                    # (R-22). При закрытом обращении откроется обращение из одних ответов.
                    answer_ids.add(message.id)
                    last_answer_at = message.sent_at
                    if material_answer:
                        # Клиент начал присылать материал: текстовая реплика компании теперь гасит состояние.
                        material_answered = True
                elif (
                    v2_message
                    and answer_series_alive
                    and requires_response(message.id) is False
                ):
                    # Пояснение к присланному серию не рвёт: следующий скан — всё ещё запрошенный файл.
                    last_answer_at = message.sent_at
                else:
                    # Самостоятельная просьба, вопрос или текст без вердикта закрывают серию ответа.
                    last_answer_at = None
                if v4_mode:
                    # v4: пределы и истёкшее ожидание проверяются у каждого открытого обращения.
                    for item, ids in list(open_v4):
                        if item.client_messages >= max_messages or (
                            message.sent_at > expired_at(item)
                        ):
                            kind = settle(item, ids, expired=True)
                            if kind:
                                closed[kind] += 1
                            v4_drop(item)
                    v4_sync()

                    label = labels.get(message.id)
                    # Отзыв просьбы (реплика со словами «не надо уже», «вопрос решился»…) решается
                    # только при метке из белого списка (`WITHDRAWAL_LABELS`) и не у просьбы,
                    # повышенной из `offline`. Реплика-отзыв остаётся в составе закрываемой работы
                    # и нового обращения не открывает.
                    withdrawal_label_ok = (
                        (label in WITHDRAWAL_LABELS or label is None)
                        and message.id not in promoted_requests
                    )
                    # Клиент отозвал просьбу при ожидании специалиста: открытое обращение ровно одно,
                    # первая реакция была, ожидание специалиста открыто. Закрытие — NO_RESPONSE_NEEDED,
                    # а не ANSWERED: специалист не отвечал, и `substantive_at` не ставится. После
                    # срока специалиста отзыв нарушение не отменяет. Автор отзыва намеренно
                    # не сверяется: открытое обращение одно.
                    if (
                        withdrawal_label_ok
                        and len(open_v4) == 1
                        and request_withdrawn(client_text)
                    ):
                        only, only_ids = open_v4[0]
                        withdrawn_after_deadline = False
                        if only.handoff_at is not None:
                            _, only_substantive, _, _ = rules_at(only.opened_at)
                            only_handoff_deadline = handoff_deadline(
                                only.handoff_at, only_substantive, calendar.at(only.opened_at)
                            )
                            withdrawn_after_deadline = (
                                only_handoff_deadline is not None
                                and message.sent_at > only_handoff_deadline
                            )
                        if (
                            only.first_reaction_at is not None
                            and only.handoff_at is not None
                            and only.substantive_at is None
                            and not withdrawn_after_deadline
                        ):
                            only.state = InteractionState.NO_RESPONSE_NEEDED
                            closed["no_response"] += 1
                            only.last_client_at = message.sent_at
                            only.client_messages += 1
                            only_ids.append(message.id)
                            v4_drop(only)
                            update_material_at.pop(only.opened_by_message_id, None)
                            current = None
                            current_ids = []
                            continue
                    # Отзыв просьбы до первой реакции и до её срока — «ответа не требовалось».
                    # Открытое обращение ровно одно, автор отзыва писал в нём (`own_client_work`).
                    # После срока отзыв просрочку не отменяет.
                    if (
                        current is not None
                        and len(open_v4) == 1
                        and withdrawal_label_ok
                        and request_withdrawn_before_reaction(client_text)
                    ):
                        only, only_ids = open_v4[0]
                        only_sla, _, _, _ = rules_at(only.opened_at)
                        only_deadline = response_deadline(
                            only.opened_at, only_sla, calendar.at(only.opened_at)
                        )
                        if (
                            only.first_reaction_at is None
                            and only.handoff_at is None
                            and own_client_work(message, only_ids)
                            and (only_deadline is None or message.sent_at <= only_deadline)
                        ):
                            only.state = InteractionState.NO_RESPONSE_NEEDED
                            closed["no_response"] += 1
                            only.last_client_at = message.sent_at
                            only.client_messages += 1
                            only_ids.append(message.id)
                            v4_drop(only)
                            update_material_at.pop(only.opened_by_message_id, None)
                            current = None
                            current_ids = []
                            continue
                    if label in (LABEL_ADDITION, LABEL_CORRECTION):
                        # Дополнять и поправлять нечего — это новая просьба. Открытая работа
                        # должна быть своей (её уже наполнял этот человек; автор неизвестен — своя);
                        # своя ищется среди всех открытых, берётся последняя из своих.
                        own_pair = None
                        for pair in open_v4:
                            if own_client_work(message, pair[1]):
                                own_pair = pair
                        own_work = own_pair is not None
                        if own_pair is not None:
                            # Дополнение присоединяется к СВОЕЙ работе:
                            # регистрация состава ниже читает `current`.
                            current, current_ids = own_pair
                        # Своей открытой работы нет, но своя работа только что закрылась не ответом
                        # по существу — это продолжение своей речи: срока оно не получает.
                        continues_own = not own_work and continues_own_speech(message)
                        if not own_work and not continues_own:
                            promoted_requests.add(message.id)
                        elif (
                            # `own_work` проверяется явно: под `continues_own` своей работы нет.
                            own_work
                            and label == LABEL_CORRECTION
                            and current.first_reaction_at is None
                        ):
                            # Поправка ДО первой реакции переносит начало
                            # срока: отвечать надо на исправленные данные,
                            # а не на те, что клиент уже отозвал.
                            current.opened_at = message.sent_at
                    if current is not None and (
                        current.substantive_at is None
                        and current.handoff_at is None
                        and requires_response(message.id) is not False
                        and current_ids
                        and all(requires_response(mid) is False for mid in current_ids)
                    ):
                        # «Спасибо»-серия не наследует срок — как в v1–v3.
                        # Прочие открытые обращения остаются в списке: их
                        # сроки к этой серии отношения не имеют.
                        current.state = InteractionState.NO_RESPONSE_NEEDED
                        closed["no_response"] += 1
                        v4_drop(current)
                        current = None
                        current_ids = []
                    elif (
                        current is not None
                        and (
                            requires_response(message.id) is True
                            or (
                                # У файла без подписи вердикта модели нет
                                # (очередь размечает только текст). После
                                # подтверждения непрошеного файла следующий
                                # неизвестный файл требует своей реакции. До
                                # подтверждения файлы — одна пачка; ответы на
                                # просьбу компании выше уже дают False.
                                bare_attachment
                                and requires_response(message.id) is None
                                and current.first_reaction_at is not None
                                # Материал к промежуточному дополнению или
                                # поправке клиента этой реакции сам не получал:
                                # пачка сохраняется.
                                and not (
                                    current.opened_by_message_id in update_material_at
                                    and message.sent_at - update_material_at[current.opened_by_message_id]
                                    <= ANSWER_ATTACHMENT_WINDOW
                                )
                            )
                        )
                        and not answers_counter_question
                    ):
                        # Просьба клиента — своё обращение и свой срок (R-18). Прежнее закрывается,
                        # только если ждать по нему некого; без реакции или с открытым вторым слоем
                        # оно остаётся в списке.
                        if current.first_reaction_at is None or (
                            current.handoff_at is not None
                            and current.substantive_at is None
                        ):
                            pass
                        else:
                            # Помощник ответил сам либо специалист уже вышел
                            # на связь — ждать нечего (v1 и v3 закрывают так же).
                            current.state = InteractionState.ANSWERED
                            closed["by_assistant"] += 1
                            v4_drop(current)
                        current = None
                        current_ids = []
                elif current is not None:
                    over_count = current.client_messages >= max_messages
                    # Разрез «спасибо»-серии проверяется раньше предела: такая серия закрывается
                    # «ответ не требовался», а не «осталось без ответа».
                    if (
                        current.substantive_at is None
                        and current.handoff_at is None
                        and requires_response(message.id) is not False
                        and current_ids
                        and all(requires_response(mid) is False for mid in current_ids)
                    ):
                        # «Спасибо»-серия не наследует срок: следующий вопрос — новое обращение.
                        # Реакция компании на такую серию разрезу не мешает; передача или ответ удерживают.
                        current.state = InteractionState.NO_RESPONSE_NEEDED
                        closed["no_response"] += 1
                        close_current()
                    elif (
                        current.version >= 3
                        and current.substantive_at is not None
                        and requires_response(message.id) is True
                        and not answers_counter_question
                    ):
                        # Специалист уже вышел на связь встречным вопросом: ответ клиента остаётся
                        # в этом обращении (v2), следующая просьба получает новый срок.
                        current.state = InteractionState.ANSWERED
                        close_current()
                    elif (
                        current.version >= 3
                        and current.handoff_at is not None
                        and current.substantive_at is None
                        and requires_response(message.id) is True
                        and not answers_counter_question
                    ):
                        # v3: новая просьба во время ожидания специалиста — своё обращение и свой срок;
                        # переданное откладывается (`parked`) и продолжает ждать специалиста.
                        parked[current.opened_by_message_id] = (current, current_ids)
                        current = None
                        current_ids = []
                    elif (
                        current.first_reaction_at is not None
                        and current.handoff_at is None
                        and current.substantive_at is None
                        and requires_response(message.id) is True
                        # После встречного вопроса компании реплика клиента — ответ, а не новая просьба.
                        and not answers_counter_question
                    ):
                        # Новая просьба после реакции помощника без передачи — новое обращение со своим
                        # сроком; старое закрывается, как в `settle` (помощник ответил сам).
                        current.state = InteractionState.ANSWERED
                        closed["by_assistant"] += 1
                        close_current()
                    elif over_count or message.sent_at > expired_at(current):
                        kind = settle(current, current_ids, expired=True)
                        if kind:
                            closed[kind] += 1
                        close_current()

                if current is None:
                    current = Interaction(
                        chat_id=chat_id,
                        thread_id=thread,
                        opened_at=message.sent_at,
                        opened_by_message_id=message.id,
                        last_client_at=message.sent_at,
                        client_messages=1,
                        state=InteractionState.OPEN,
                        version=engine_version_at(message.sent_at),
                    )
                    if persist:
                        session.add(current)
                    current_ids = [message.id]
                    open_tracker.append((current, current_ids))
                    if v4_mode or current.version >= 4:
                        # v4: новое обращение встаёт в конец списка открытых и становится текущим;
                        # состав — тот же список, что в `open_tracker`.
                        # Важно: условие — версия нового обращения, а не режим чата: первое обращение v4,
                        # родившееся на старой ветке, иначе не попало бы в `open_v4` и осталось сиротой.
                        open_v4.append((current, current_ids))
                    built += 1
                else:
                    current.last_client_at = message.sent_at
                    current.client_messages += 1
                    current_ids.append(message.id)

                if v4_mode:
                    opener = current.opened_by_message_id
                    if labels.get(message.id) in (LABEL_ADDITION, LABEL_CORRECTION):
                        update_material_at[opener] = message.sent_at
                    elif (bare_attachment and opener in update_material_at
                          and message.sent_at - update_material_at[opener] <= ANSWER_ATTACHMENT_WINDOW):
                        update_material_at[opener] = message.sent_at
                    else:
                        update_material_at.pop(opener, None)

            else:  # COMPANY
                if message.id in not_staff_messages:
                    # Подпись «это не сотрудник» (робот, чужой бот) — не ответ клиенту. Подпись
                    # «Система» так не помечается: под ней пишут живые сотрудники.
                    continue

                # Версия правил для реплики компании: у открытого обращения — его собственная,
                # иначе по моменту сообщения.
                v2_rules = (
                    current.version if current is not None else engine_version_at(message.sent_at)
                ) >= 2
                label = labels.get(message.id)
                # Компания спрашивает клиента: метка question, а в v2 ещё и «?» в конце реплики —
                # вопрос вслед за ответом по существу модель помечает substantive.
                asks_client = label == "question" or (
                    v2_rules and strip_integrator_prefix(message.text).rstrip().endswith("?")
                )
                company_text = strip_integrator_prefix(message.text) or ""
                if company_text:
                    last_company_text = company_text

                if v2_rules and is_integrator_notice(message):
                    # Ответ интегратора на /auth, /info, привязку чата — не реакция и не ход к клиенту.
                    continue

                if v2_rules and message.id in broadcast_ids:
                    # Рассылка — не реакция и не ответ, но вопрос в ней передаёт ход клиенту.
                    if asks_client:
                        awaiting_at = message.sent_at
                    continue

                # Вложение компании без текста ход клиенту не возвращает (v2). Текст — после снятия
                # префикса «Имя [домен] делится файлом».
                keeps_turn = v2_rules and not strip_integrator_prefix(message.text)
                # v4: реплика `other` или чистое приветствие окно «ждём клиента» не гасит и не взводит.
                inert_reply = v4_mode and (
                    label == LABEL_OTHER or is_greeting_only(message)
                )
                if v2_rules and not keeps_turn and not inert_reply:
                    # v2: вопрос или просьба компании взводит «ждём клиента» и без открытого обращения;
                    # любая другая реплика компании ход возвращает.
                    awaiting_at = message.sent_at if asks_client else None
                    # Просьба прислать материал взводит (или переустанавливает) `material_at`.
                    # Гаснет состояние по сроку или от текстовой реплики компании после того, как клиент
                    # начал присылать материал; до этого дописки и файлы компании его не трогают.
                    # Метка не читается: такая просьба бывает размечена `substantive`.
                    if v4_mode:
                        if material_request(company_text):
                            material_at = message.sent_at
                            material_answered = False
                        elif material_answered:
                            material_at = None
                            material_answered = False

                if v4_mode:
                    # v4: чистое приветствие (R-19) и `other` ничего не закрывают.
                    if label == LABEL_OTHER or is_greeting_only(message):
                        # `other` (не приветствие) — первая реакция открытым обращениям без реакции,
                        # как `ack`. Больше ничего: окно «ждём клиента» не трогается, ответ, передачу
                        # и `update_material_at` такая реплика не меняет.
                        if label == LABEL_OTHER and not is_greeting_only(message):
                            other_staff_id = attribution_map.get(message.id)
                            for other_item, _other_ids in open_v4:
                                if other_item.first_reaction_at is not None:
                                    continue
                                other_cfg = calendar.at(other_item.opened_at)
                                other_sla, _, _, _ = rules_at(other_item.opened_at)
                                other_item.first_reaction_at = message.sent_at
                                other_item.first_reaction_message_id = message.id
                                other_item.first_reaction_staff_id = other_staff_id
                                other_item.ttfr_seconds = int(
                                    (message.sent_at - other_item.opened_at)
                                    .total_seconds()
                                )
                                other_item.ttfr_business_seconds = business_seconds(
                                    other_item.opened_at, message.sent_at, other_cfg
                                )
                                other_deadline = response_deadline(
                                    other_item.opened_at, other_sla, other_cfg
                                )
                                other_item.sla_breached = (
                                    other_deadline is not None
                                    and message.sent_at > other_deadline
                                )
                                # Важно: состояние обязательно: кандидатов первого слоя `alerts.py`
                                # берёт по `state is OPEN`, а не по пустой первой реакции.
                                other_item.state = InteractionState.REACTED
                        # `other` установленного сотрудника, не делавшего передачу, при ровно одном
                        # ожидании специалиста — выход специалиста на связь (R-21). Ссылка, если есть,
                        # должна попадать в это ожидание.
                        if label == LABEL_OTHER and not is_greeting_only(message):
                            reply_staff = attribution_map.get(message.id)
                            reply_link = links.get(message.id)
                            waiting = [
                                pair for pair in open_v4
                                if pair[0].handoff_at is not None
                                and pair[0].substantive_at is None
                                and message.sent_at >= pair[0].handoff_at
                            ]
                            if len(waiting) == 1:
                                wait_item, wait_ids = waiting[0]
                                if (
                                    reply_staff is not None
                                    and wait_item.handoff_staff_id is not None
                                    and reply_staff != wait_item.handoff_staff_id
                                    and (reply_link is None
                                         or reply_link == wait_item.opened_by_message_id
                                         or reply_link in wait_ids)
                                ):
                                    credit_specialist_contact(wait_item, message, reply_staff)
                                    wait_item.state = InteractionState.ANSWERED
                                    v4_drop(wait_item)
                                    v4_sync()
                        continue
                    if not open_v4:
                        continue  # ответ без обращения — не эпизод

                    # «Ждём клиента» уже обновлено выше по общему правилу v2:
                    # в v4 оно не зависит от того, есть ли открытое обращение.
                    _, substantive_verdict = verdicts.get(message.id, (None, None))
                    is_handoff = label == LABEL_HANDOFF
                    # Содержательность — как в v1–v3: `promise` приходит с is_substantive=false.
                    # Строка вердикта есть, а `is_substantive` пуст — не ответ по существу; полное
                    # отсутствие вердикта содержательно. Первой реакцией реплика остаётся.
                    blank_substantive = (
                        message.id in verdicts
                        and substantive_verdict is None
                    )
                    is_substantive = (
                        not is_handoff
                        and substantive_verdict is not False
                        and not blank_substantive
                    ) or message.media_kind in SUBSTANTIVE_MEDIA
                    staff_id = attribution_map.get(message.id)

                    # Обращения, где клиенту было НЕ на что отвечать, эта
                    # реплика закрывает без реакции — как в v1–v3, только
                    # проверка своя у каждого открытого обращения.
                    for item, ids in list(open_v4):
                        if not any(requires_response(mid) is not False for mid in ids):
                            kind = settle(item, ids, expired=False)
                            if kind:
                                closed[kind] += 1
                            v4_drop(item)
                    v4_sync()
                    if not open_v4:
                        continue

                    def specialist_reply_to(item: Interaction) -> bool:
                        """Вышел ли специалист на связь по этому ожиданию.

                        После передачи, другой сотрудник; сама передача не в счёт. Неатрибутированный
                        автор (при `version >= 2`) засчитывается, как в v1–v3.
                        """
                        attributed = (
                            staff_id is not None
                            and staff_id != item.handoff_staff_id
                        ) or (staff_id is None and item.version >= 2)
                        return (
                            item.handoff_at is not None
                            and item.substantive_at is None
                            and not is_handoff
                            and message.sent_at >= item.handoff_at
                            and attributed
                        )

                    def batch_sibling_waiting(target: Interaction):
                        """Единственное ожидание специалиста из той же пачки, что `target`, или None.

                        Вызывается до раздачи первых реакций этой репликой, иначе пачки совпали бы всегда.
                        """
                        if target.handoff_at is not None and target.substantive_at is None:
                            return None  # ссылка пришла ровно туда, где ждут
                        waiting = [
                            pair for pair in open_v4
                            if pair[0].handoff_at is not None
                            and pair[0].substantive_at is None
                            and message.sent_at >= pair[0].handoff_at
                        ]
                        if len(waiting) != 1:
                            return None  # выбирать не из чего — только один (R-21)
                        only = waiting[0]
                        anchor = target.first_reaction_message_id
                        if anchor is None or only[0].first_reaction_message_id != anchor:
                            return None  # разные пачки: соседство не доказано
                        return only

                    def waiting_specialists():
                        """Незакрытые ожидания специалиста на момент этой реплики."""
                        return [
                            pair for pair in open_v4
                            if pair[0].handoff_at is not None
                            and pair[0].substantive_at is None
                            and message.sent_at >= pair[0].handoff_at
                        ]

                    def single_handoff_target():
                        """Кому передача без ссылки открывает второй слой: единственному открытому
                        обращению без передачи — или никому: при двух и больше движок не угадывает (R-21).
                        """
                        free = [pair for pair in open_v4 if pair[0].handoff_at is None]
                        if len(free) != 1:
                            return []
                        # Передача — первая реакция этому обращению (адресация та же, что у первой
                        # реакции); после уже данной реакции передача может быть о другом.
                        if free[0][0].first_reaction_at is not None:
                            return []
                        # Просьба о звонке (`offline`) — ответ вне чата, слой не открываем.
                        if any(labels.get(mid) == "offline" for mid in free[0][1]):
                            return []
                        return free

                    def closed_batch_sibling(missed_link: int):
                        """Ссылка попала в уже закрытого соседа по пачке — единственное ожидание
                        специалиста с той же первой реакцией или None.
                        """
                        anchor = closed_batch_anchor.get(missed_link)
                        if anchor is None:
                            return None
                        waiting = waiting_specialists()
                        if len(waiting) != 1:
                            return None
                        if waiting[0][0].first_reaction_message_id != anchor:
                            return None
                        return waiting[0]

                    link = links.get(message.id)
                    linked = None
                    if link is not None:
                        # Модель ссылается на сообщение, которое видит во входе; повтор вопроса или
                        # досланный файл входят в ту же работу, поэтому ищем и в составе.
                        for pair in open_v4:
                            if pair[0].opened_by_message_id == link:
                                linked = pair
                                break
                        if linked is None:
                            for pair in open_v4:
                                if link in pair[1]:
                                    linked = pair
                                    break
                    # Второй слой адресуется ссылкой модели. Ссылка на работу, которой среди открытых
                    # нет, — для второго слоя «адресовано не этим работам».
                    link_missed = link is not None and linked is None
                    # Сосед по пачке: ссылка попала в соседнее обращение той же пачки (или в уже
                    # закрытое), а специалиста ждёт другое — ему засчитывается ответ ниже.
                    sibling = None
                    if link_missed:
                        handoff_targets = []
                        answer_target = None
                        # Ссылка в уже закрытого соседа по пачке.
                        if not is_handoff:
                            sibling = closed_batch_sibling(link)
                    elif linked is not None:
                        # Ссылка модели — явная цель: повторная передача по уже
                        # переданной просьбе адресуется ей; условие на `handoff_at`
                        # ниже сохраняет исходный срок специалиста.
                        # Адресный ответ: передача и закрытие — только указанной просьбе.
                        handoff_targets = [linked]
                        answer_target = linked
                        # Ссылка попала в соседа по пачке без ожидания специалиста, а незакрытое
                        # ожидание в чате ровно одно и с той же первой реакцией — засчитать ему
                        # (`batch_sibling_waiting`). Передача соседу не раздаётся.
                        sibling = batch_sibling_waiting(linked[0])

                    else:
                        # Передача без ссылки открывает второй слой, только когда угадывать не нужно
                        # (`single_handoff_target`, ниже): срок специалиста нельзя повесить на угаданную
                        # тему (R-21).
                        handoff_targets = []
                        # Без ссылки закрывать можно, только когда выбирать
                        # НЕ ИЗ ЧЕГО — ожидание специалиста ровно одно.
                        # При двух и больше движок не угадывает (R-21).
                        answer_target = None
                        waiting_specialist = [
                            pair for pair in open_v4
                            if pair[0].handoff_at is not None
                            and pair[0].substantive_at is None
                            and message.sent_at >= pair[0].handoff_at
                        ]
                        if len(waiting_specialist) == 1 and not is_handoff:
                            only = waiting_specialist[0]
                            # Выход на связь другого сотрудника (R-21). `substantive` без ссылки второй
                            # слой не закрывает; результат передавшего — только со ссылкой.
                            if specialist_reply_to(only[0]):
                                answer_target = only
                        # Передача без ссылки, а передавать некому, кроме одного обращения.
                        if is_handoff:
                            handoff_targets = single_handoff_target()

                    # Первый слой адресуется метаданными Telegram для любой метки. Нет reply —
                    # реакция всем открытым обращениям (даже если ссылка модели указывает на закрытую
                    # работу); есть reply — только той работе, куда он попал. Второй слой не трогается.
                    if message.reply_to_tg_message_id is None:
                        reaction_targets = list(open_v4)
                    else:
                        replied = tg_to_id.get(message.reply_to_tg_message_id)
                        reaction_targets = [
                            pair for pair in open_v4
                            if replied is not None and (
                                pair[0].opened_by_message_id == replied
                                or replied in pair[1]
                            )
                        ]
                        # Reply мимо всех открытых работ (на закрытую работу или своё сообщение)
                        # читается как реплика без reply — реакция всем открытым.
                        if not reaction_targets:
                            reaction_targets = list(open_v4)

                    for item, ids in reaction_targets:
                        update_material_at.pop(item.opened_by_message_id, None)
                        if item.first_reaction_at is not None:
                            continue
                        case_cfg = calendar.at(item.opened_at)
                        case_sla, _, _, _ = rules_at(item.opened_at)
                        item.first_reaction_at = message.sent_at
                        item.first_reaction_message_id = message.id
                        item.first_reaction_staff_id = staff_id
                        item.ttfr_seconds = int(
                            (message.sent_at - item.opened_at).total_seconds()
                        )
                        item.ttfr_business_seconds = business_seconds(
                            item.opened_at, message.sent_at, case_cfg
                        )
                        deadline = response_deadline(item.opened_at, case_sla, case_cfg)
                        item.sla_breached = (
                            deadline is not None and message.sent_at > deadline
                        )
                        item.state = InteractionState.REACTED

                    if is_handoff:
                        for item, ids in handoff_targets:
                            # По просьбам позвонить второй слой не судится: звонок идёт вне чата.
                            if call_only_work(ids):
                                continue
                            if item.handoff_at is None:
                                item.handoff_at = message.sent_at
                                item.handoff_staff_id = staff_id

                    if answer_target is not None:
                        item, ids = answer_target
                        specialist_replied = specialist_reply_to(item)
                        # Встречный вопрос сотрудника клиенту (`asks_client`) выполняет ожидание
                        # специалиста; за ответом клиента движок не следит. Со ссылкой автор не важен,
                        # без ссылки — только выход на связь другого сотрудника (`specialist_reply_to`).
                        # Важно: передача, заканчивающаяся вопросом клиенту, закрывает свой второй
                        # слой сразу (см. тест
                        # `test_a_handoff_that_asks_the_client_back_fulfils_its_own_wait`
                        # в `tests/test_engine_v4_requested_material.py`).
                        counter_question = (
                            asks_client
                            and link is not None
                            and item.handoff_at is not None
                            and item.substantive_at is None
                            and message.sent_at >= item.handoff_at
                        )
                        if (is_substantive or specialist_replied or counter_question) and (
                            item.substantive_at is None
                        ):
                            case_cfg = calendar.at(item.opened_at)
                            _, case_substantive, _, _ = rules_at(item.opened_at)
                            item.substantive_at = message.sent_at
                            item.substantive_message_id = message.id
                            item.substantive_staff_id = staff_id
                            item.ttfa_seconds = int(
                                (message.sent_at - item.opened_at).total_seconds()
                            )
                            item.ttfa_business_seconds = business_seconds(
                                item.opened_at, message.sent_at, case_cfg
                            )
                            item.substantive_breached = _handoff_breached(
                                item.handoff_at, message.sent_at, case_substantive, case_cfg
                            )
                        # Встречный вопрос специалиста ожидание выполняет, но
                        # обращение не закрывает: ход за клиентом (v1–v3).
                        if is_substantive or (specialist_replied and label != "question"):
                            item.state = InteractionState.ANSWERED
                            v4_drop(item)

                    if sibling is not None and not is_handoff:
                        # Соседу засчитываются встречный вопрос со ссылкой и результат по существу
                        # другого специалиста — установленного сотрудника, не делавшего передачу (R-21).
                        # Условие выписано явно, а не через `specialist_reply_to`: та принимает
                        # и неизвестного автора. Простой выход на связь соседу не засчитывается.
                        # Реплику `handoff` сюда не пускаем: передача по одной просьбе не гасит срок
                        # другой (R-21).
                        item, ids = sibling
                        other_specialist = (
                            staff_id is not None
                            and item.handoff_staff_id is not None
                            and staff_id != item.handoff_staff_id
                        )
                        sibling_substantive = is_substantive and other_specialist
                        sibling_question = (
                            asks_client
                            and link is not None
                            and item.handoff_at is not None
                            and item.substantive_at is None
                            and message.sent_at >= item.handoff_at
                        )
                        if (
                            sibling_substantive or sibling_question
                        ) and item.substantive_at is None:
                            case_cfg = calendar.at(item.opened_at)
                            _, case_substantive, _, _ = rules_at(item.opened_at)
                            item.substantive_at = message.sent_at
                            item.substantive_message_id = message.id
                            item.substantive_staff_id = staff_id
                            item.ttfa_seconds = int(
                                (message.sent_at - item.opened_at).total_seconds()
                            )
                            item.ttfa_business_seconds = business_seconds(
                                item.opened_at, message.sent_at, case_cfg
                            )
                            item.substantive_breached = _handoff_breached(
                                item.handoff_at, message.sent_at, case_substantive, case_cfg
                            )
                        # Встречный вопрос ожидание выполняет, но обращение не закрывает.
                        if sibling_substantive:
                            item.state = InteractionState.ANSWERED
                            v4_drop(item)
                    v4_settle_finished()
                    v4_sync()
                    continue

                if current is None:
                    continue  # ответ без обращения — не эпизод

                # Клиенту было НА ЧТО отвечать? Неразмеченное сообщение
                # (None) считается вопросом — перекос в пользу клиента, как
                # и везде в движке. Общее для двух правил ниже.
                had_question = any(
                    requires_response(mid) is not False for mid in current_ids
                )
                if not had_question:
                    # Ни одно слово клиента ответа не требовало — реплика компании не реакция:
                    # без ttfr и просрочки, закрытие тем же `settle` («ответа не требовалось»).
                    kind = settle(current, current_ids, expired=False)
                    if kind:
                        closed[kind] += 1
                    close_current()
                    continue

                _, substantive_verdict = verdicts.get(message.id, (None, None))
                if not keeps_turn:
                    awaiting_at = message.sent_at if asks_client else None

                # Передача специалисту — единственное, что открывает второй слой SLA.
                is_handoff = label == LABEL_HANDOFF

                # Без вердикта ответ считается содержательным: «отписка» требует явного вердикта ИИ.
                # Присланный документ — ответ сам по себе, какой бы ни была подпись; стикер и гиф — нет.
                is_substantive = (
                    not is_handoff and substantive_verdict is not False
                ) or message.media_kind in SUBSTANTIVE_MEDIA
                staff_id = attribution_map.get(message.id)
                # График и пороги — одни на все метрики и сроки обращения, на момент его открытия.
                case_cfg = calendar.at(current.opened_at)
                case_sla, case_substantive, _, _ = rules_at(current.opened_at)

                if current.first_reaction_at is None:
                    current.first_reaction_at = message.sent_at
                    current.first_reaction_message_id = message.id
                    current.first_reaction_staff_id = staff_id
                    current.ttfr_seconds = int(
                        (message.sent_at - current.opened_at).total_seconds()
                    )
                    current.ttfr_business_seconds = business_seconds(
                        current.opened_at, message.sent_at, case_cfg
                    )
                    # Нарушение — по тому же сроку, по которому шлётся алерт
                    # (полный порог с начала нового дня), иначе отчёт покажет
                    # просрочки, которых по правилам не было.
                    deadline = response_deadline(
                        current.opened_at, case_sla, case_cfg
                    )
                    current.sla_breached = (
                        deadline is not None and message.sent_at > deadline
                    )
                    current.state = InteractionState.REACTED

                # Передача открывает второй слой; запоминается первая — повтор срок не продлевает.
                # Важно: только если клиенту было на что отвечать: «передала информацию» после уведомления
                # клиента — внутренняя пересылка, а не долг специалиста.
                if is_handoff and current.handoff_at is None and had_question:
                    current.handoff_at = message.sent_at
                    current.handoff_staff_id = staff_id

                # Второй слой закрывает сам выход специалиста на связь («увидел и принял» или по
                # существу). Специалист — тот, кто пишет после передачи и не является передавшим.
                specialist_replied = (
                    current.handoff_at is not None
                    and not is_handoff
                    # Не строго: порядок (sent_at, id), ответ в ту же секунду — всё равно после передачи.
                    and message.sent_at >= current.handoff_at
                    and (
                        (staff_id is not None and staff_id != current.handoff_staff_id)
                        # v2: автор не определён (подпись «Система», учётка без сотрудника) — компания
                        # всё же вышла на связь; в личную статистику не попадает (R-11).
                        or (staff_id is None and current.version >= 2)
                    )
                )

                # Встречный вопрос специалиста выполняет срок специалиста, но обращение не закрывает:
                # ход за клиентом, и его ответ должен присоединиться сюда.
                closes_now = is_substantive or (specialist_replied and label != "question")

                if (is_substantive or specialist_replied) and current.substantive_at is None:
                    current.substantive_at = message.sent_at
                    current.substantive_message_id = message.id
                    current.substantive_staff_id = staff_id
                    # Время до ответа по существу меряется от обращения
                    # клиента: столько он ждал, независимо от того, кто внутри
                    # компании кому что передавал.
                    current.ttfa_seconds = int(
                        (message.sent_at - current.opened_at).total_seconds()
                    )
                    current.ttfa_business_seconds = business_seconds(
                        current.opened_at, message.sent_at, case_cfg
                    )
                    # А НАРУШЕНИЕ — против обещания, то есть от момента
                    # передачи. Не было передачи — нет и второго слоя:
                    # помощник ответил сам, специалисту ничего не обещали.
                    current.substantive_breached = _handoff_breached(
                        current.handoff_at,
                        message.sent_at,
                        case_substantive,
                        case_cfg,
                    )

                if closes_now:
                    current.state = InteractionState.ANSWERED
                    close_current()

    # Финальный проход по обращениям, дожившим открытыми; решает тот же `settle`.
    for interaction, client_ids in open_tracker:
        if interaction.state not in (InteractionState.OPEN, InteractionState.REACTED):
            continue
        expired = now > expired_at(interaction)
        # Префикс сохраняет недавние подтверждения для последующих дополнений и
        # передач, но истёкшее историческое ожидание не должно занимать номер,
        # который модель может назвать.
        if not settle_open and not (before is not None and expired):
            continue
        kind = settle(
            interaction,
            client_ids,
            expired=expired,
        )
        if kind:
            closed[kind] += 1

    log.info(
        "episodes.rebuilt",
        chats=len(chat_ids),
        interactions=built,
        no_response=closed["no_response"],
        by_assistant=closed["by_assistant"],
        abandoned=closed["abandoned"],
    )
    result: dict = {
        "chats": len(chat_ids),
        "interactions": built,
        "abandoned": closed["abandoned"],
        "by_assistant": closed["by_assistant"],
    }
    if not persist:
        # Эпизоды никуда не записаны — единственный способ их увидеть.
        result["items"] = [interaction for interaction, _ in open_tracker]
        # Состав обращений и признанные ответами сообщения — для разбора.
        result["members"] = [(interaction, list(ids)) for interaction, ids in open_tracker]
        result["answers"] = sorted(answer_ids)
        if replay_input is not None:
            # То же распознавание рассылок — для фильтра хвоста классификации.
            result["broadcast_ids"] = sorted(broadcast_ids)
            # Префикс потока может держать заготовку «ответа не требовалось» до
            # окончательного разбора. Кандидаты контекста берут действующие флаги
            # движка, включая повышенные до просьбы поправки и ответы клиента.
            result["response_required_ids"] = sorted({
                mid for _, ids in open_tracker for mid in ids
                if requires_response(mid) is not False
            })
    return result
