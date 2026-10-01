"""Анонимный админ группы — сторона компании (правило стороны версии 4).

Сообщения «от имени группы» Telegram присылает от служебной учётки
GroupAnonymousBot (id 1087968824, is_bot=true), а `sender_chat` равен самой
группе. Писать так может только админ, а админы этих групп — бухгалтерия:
автор остаётся неизвестным, но сторона известна точно."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    BusinessSide,
    Chat,
    ChatState,
    Classification,
    Interaction,
    InteractionState,
    Message,
    TransportActorKind,
)
from app.services.episodes import rebuild_interactions
from app.services.ingestion import (
    ANONYMOUS_ADMIN_BOT_ID,
    SIDE_RULE_VERSION,
    resolve_business_side,
    resolve_transport_actor,
)
from app.services.tracking import open_period
from app.services.verdicts import SOURCE_MODEL
from tests.conftest import requires_db

MSK = timezone(timedelta(hours=3))

# Среда — рабочий день при календаре по умолчанию.
ASKED = datetime(2026, 9, 16, 10, 0, tzinfo=MSK)
COUNTER = datetime(2026, 9, 16, 10, 5, tzinfo=MSK)
CODE = datetime(2026, 9, 16, 10, 20, tzinfo=MSK)
DONE = datetime(2026, 9, 16, 11, 30, tzinfo=MSK)
THANKS = datetime(2026, 9, 16, 11, 31, tzinfo=MSK)
NOW = datetime(2026, 9, 16, 12, 0, tzinfo=MSK)

CLIENT_TG = 770001
OTHER_BOT_TG = 770002


def _anonymous_update(chat_id: int, text: str) -> dict:
    """Сырой апдейт «от имени группы» — как его присылает Telegram."""
    return {
        "message_id": 1,
        "chat": {"id": chat_id, "type": "supergroup", "title": "Бухгалтерия: ООО Ромашка"},
        "from": {
            "id": ANONYMOUS_ADMIN_BOT_ID,
            "is_bot": True,
            "first_name": "Group",
            "username": "GroupAnonymousBot",
        },
        "sender_chat": {"id": chat_id, "type": "supergroup", "title": "Бухгалтерия: ООО Ромашка"},
        "date": int(ASKED.timestamp()),
        "text": text,
    }


# ── Приём: сторона по отправителю ────────────────────────────────────────


async def test_anonymous_admin_is_the_company_side():
    """Сырой апдейт анонимного админа: транспорт — чужой бот, сторона — компания."""
    raw = _anonymous_update(-1009160001, "Просьба прислать код из банка")
    actor, tg_user_id = resolve_transport_actor(raw)
    assert actor is TransportActorKind.OTHER_BOT, "транспорт остаётся ботом — это факт апдейта"
    assert tg_user_id == ANONYMOUS_ADMIN_BOT_ID
    # Сессия не нужна: решение принимается по одному только id отправителя.
    side = await resolve_business_side(None, actor, tg_user_id, raw["text"])
    assert side is BusinessSide.COMPANY, "сообщение «от имени группы» осталось ничьим"


async def test_other_bots_stay_unknown():
    """Регресс: прочие чужие боты по-прежнему ничьи — кто за ними, мы не знаем."""
    raw = {
        "message_id": 2,
        "chat": {"id": -1009160001, "type": "supergroup"},
        "from": {"id": OTHER_BOT_TG, "is_bot": True, "username": "SomeOtherBot"},
        "date": int(ASKED.timestamp()),
        "text": "Ваша заявка принята",
    }
    actor, tg_user_id = resolve_transport_actor(raw)
    assert actor is TransportActorKind.OTHER_BOT
    assert await resolve_business_side(None, actor, tg_user_id, raw["text"]) is BusinessSide.UNKNOWN


# ── Пересчёт истории ─────────────────────────────────────────────────────


@requires_db
async def test_recompute_turns_history_into_company(session):
    """Накопленные 80 сообщений становятся компанией при старте воркера.

    Сторона меняется с `unknown`, а не между клиентом и компанией, поэтому
    вердикт соседа не стирается и в переспрос никто не уходит: у самих
    анонимных сообщений вердикта нет, их подберёт обычная очередь.
    """
    from app.services.ai_stats import pending_conditions
    from app.services.reprocess import recompute_sides

    chat = Chat(tg_chat_id=-1009160002, title="Бухгалтерия: ООО Ромашка", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    anonymous = Message(
        chat_id=chat.id, tg_message_id=1, tg_user_id=ANONYMOUS_ADMIN_BOT_ID,
        transport_actor_kind=TransportActorKind.OTHER_BOT,
        business_side=BusinessSide.UNKNOWN, side_rule_version=3,
        text="Выписки с 01.09 по 15.09 проведены", char_count=34,
        sent_at=DONE, needs_reclassification=False,
    )
    stranger = Message(
        chat_id=chat.id, tg_message_id=2, tg_user_id=OTHER_BOT_TG,
        transport_actor_kind=TransportActorKind.OTHER_BOT,
        business_side=BusinessSide.UNKNOWN, side_rule_version=3,
        text="Ваша заявка принята", char_count=19, sent_at=DONE,
    )
    client = Message(
        chat_id=chat.id, tg_message_id=3, tg_user_id=CLIENT_TG,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT, side_rule_version=3,
        text="Проведите выписки", char_count=17, sent_at=ASKED,
        needs_reclassification=False,
    )
    session.add_all([anonymous, stranger, client])
    await session.flush()
    session.add(
        Classification(
            message_id=client.id, model=get_settings().ai_model, prompt_version=14,
            source=SOURCE_MODEL, label="request", requires_response=True,
        )
    )
    await session.flush()

    result = await recompute_sides(session)
    assert result["reclassify"] == 0, "переворота клиент↔компания не было — переспрашивать нечего"

    for message in (anonymous, stranger, client):
        await session.refresh(message)
    assert anonymous.business_side is BusinessSide.COMPANY
    assert anonymous.transport_actor_kind is TransportActorKind.OTHER_BOT, "транспорт — факт, он не меняется"
    assert anonymous.side_rule_version == SIDE_RULE_VERSION
    assert anonymous.needs_reclassification is False
    assert stranger.business_side is BusinessSide.UNKNOWN, "чужой бот остался ничьим"
    assert client.business_side is BusinessSide.CLIENT
    survived = await session.scalar(
        select(Classification.id).where(Classification.message_id == client.id)
    )
    assert survived is not None, "вердикт соседнего сообщения стёрся на ровном месте"

    # Вердикта у анонимного сообщения нет — очередь классификации заберёт
    # его сама, отдельного возврата в очередь не требуется.
    pending = list(
        await session.scalars(
            select(Message.id).where(*pending_conditions(get_settings().ai_accepted_models))
        )
    )
    assert anonymous.id in pending, "анонимное сообщение не попало в очередь классификации"
    assert stranger.id not in pending


@requires_db
async def test_anonymous_admin_is_not_offered_as_unknown_bot(session):
    """В «кто пишет в чатах» анонимный админ не значится: опознавать нечего."""
    from app.services.reprocess import unknown_bot_senders

    chat = Chat(tg_chat_id=-1009160003, title="Бухгалтерия", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    session.add_all(
        [
            Message(
                chat_id=chat.id, tg_message_id=1, tg_user_id=ANONYMOUS_ADMIN_BOT_ID,
                transport_actor_kind=TransportActorKind.OTHER_BOT,
                business_side=BusinessSide.COMPANY, text="Проведены", char_count=9, sent_at=DONE,
            ),
            Message(
                chat_id=chat.id, tg_message_id=2, tg_user_id=OTHER_BOT_TG,
                transport_actor_kind=TransportActorKind.OTHER_BOT,
                business_side=BusinessSide.UNKNOWN, text="Заявка", char_count=6, sent_at=DONE,
            ),
        ]
    )
    await session.flush()

    senders = {row["tg_user_id"] for row in await unknown_bot_senders(session)}
    assert ANONYMOUS_ADMIN_BOT_ID not in senders
    assert OTHER_BOT_TG in senders


# ── Движок: переписка с анонимными репликами ────────────────────────────


async def _say(
    session, chat, tg_id: int, side: BusinessSide, sent_at, text: str, *,
    label: str, anonymous: bool = False, **verdict,
) -> Message:
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_id,
        transport_actor_kind=(
            TransportActorKind.OTHER_BOT if anonymous else TransportActorKind.HUMAN_USER
        ),
        tg_user_id=ANONYMOUS_ADMIN_BOT_ID if anonymous else CLIENT_TG,
        business_side=side,
        text=text,
        char_count=len(text),
        sent_at=sent_at,
    )
    session.add(message)
    await session.flush()
    session.add(
        Classification(
            message_id=message.id, model=get_settings().ai_model, prompt_version=14,
            source=SOURCE_MODEL, label=label, **verdict,
        )
    )
    await session.flush()
    return message


async def _anonymous_admin_dialogue(
    session, tg_chat_id: int, *, anonymous_side: BusinessSide
) -> tuple[Chat, Message]:
    """Переписка клиента с анонимным админом; сторона анонимных реплик — параметр теста."""
    chat = Chat(
        tg_chat_id=tg_chat_id, title="Бухгалтерия: Ромашка+Вектор+Орбита", state=ChatState.TRACKED
    )
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=ASKED - timedelta(days=7))

    await _say(
        session, chat, 1, BusinessSide.CLIENT, ASKED,
        "Добрый день! Проведите, пожалуйста, выписки с 01.09 по 15.09",
        label="request", requires_response=True,
    )
    await _say(
        session, chat, 2, anonymous_side, COUNTER, "Просьба прислать код из банка",
        label="question", anonymous=True, is_substantive=False,
    )
    await _say(
        session, chat, 3, BusinessSide.CLIENT, CODE, "551204",
        label="answer", requires_response=False,
    )
    done = await _say(
        session, chat, 4, anonymous_side, DONE, "Выписки с 01.09 по 15.09 проведены",
        label="substantive", anonymous=True, is_substantive=True,
    )
    await _say(
        session, chat, 5, BusinessSide.CLIENT, THANKS, "Спасибо!",
        label="social", requires_response=False,
    )
    return chat, done


def _v4(monkeypatch) -> None:
    """Правила движка v2–v4 включены за 30 дней до переписки."""
    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False, raising=False)
    for name in ("episode_rules_v2_since", "episode_rules_v3_since", "episode_rules_v4_since"):
        monkeypatch.setattr(get_settings(), name, ASKED - timedelta(days=30), raising=False)


@requires_db
async def test_anonymous_replies_close_the_case(session, monkeypatch):
    """Анонимные реплики менеджера — реакция и ответ по существу."""
    _v4(monkeypatch)
    chat, _ = await _anonymous_admin_dialogue(session, -1009160004, anonymous_side=BusinessSide.COMPANY)
    await rebuild_interactions(session, now=NOW)
    await session.flush()

    episodes = list(
        await session.scalars(
            select(Interaction).where(Interaction.chat_id == chat.id).order_by(Interaction.opened_at)
        )
    )
    assert len(episodes) == 1, "переписка одна: обращение клиента и работа по нему"
    episode = episodes[0]
    assert episode.opened_at == ASKED
    assert episode.first_reaction_at == COUNTER, "встречный вопрос менеджера — реакция"
    assert episode.ttfr_seconds == 300
    assert episode.sla_breached is False, "алерт «компания не отвечала» на отвеченном обращении"
    assert episode.state is InteractionState.ANSWERED
    # Времени ответа по существу тут нет и по правилам v4 быть не должно:
    # второй слой адресуется ссылкой модели, а передачи специалисту не было.
    # Закрывает обращение правило «реакция была, передачи не было —
    # помощник ведёт вопрос сам».
    assert episode.substantive_at is None


@requires_db
async def test_control_unknown_side_reproduces_the_false_alert(session, monkeypatch):
    """Контроль: со стороной `unknown` воспроизводится ложный алерт.

    Тест падал бы, если бы дело было не в стороне.
    """
    _v4(monkeypatch)
    chat, _ = await _anonymous_admin_dialogue(session, -1009160005, anonymous_side=BusinessSide.UNKNOWN)
    await rebuild_interactions(session, now=NOW)
    await session.flush()

    episode = await session.scalar(
        select(Interaction).where(Interaction.chat_id == chat.id).order_by(Interaction.opened_at)
    )
    assert episode.opened_at == ASKED
    assert episode.first_reaction_at is None, "именно так и родился алерт «Компания не отвечала»"
    assert episode.substantive_at is None
    assert episode.state is InteractionState.OPEN, "обращение висит открытым — по нему и летит алерт"


@requires_db
async def test_alert_transcript_finds_the_anonymous_reply(session, monkeypatch):
    """Текст алерта показывает последнюю реплику компании — анонимную тоже."""
    from app.services.transcript import last_company_message

    _v4(monkeypatch)
    chat, done = await _anonymous_admin_dialogue(session, -1009160006, anonymous_side=BusinessSide.COMPANY)
    found = await last_company_message(session, chat.id, None, use_thread=False)
    assert found is not None, "последней реплики компании не нашлось — алерт врал бы молчанием"
    message, author = found
    assert message.id == done.id
    assert author is None, "имени в анонимном сообщении нет и взяться ему неоткуда"


# ── Качество данных ──────────────────────────────────────────────────────


@requires_db
async def test_anonymous_message_is_not_counted_as_author_less(session):
    """«Исходящих без автора» анонимные сообщения не считают.

    Строки атрибуции у них нет и быть не может: имени Telegram не передаёт,
    в очередь разметки такое не попадает. Считать это качеством данных
    значило бы вечно звать в очередь, где пусто.
    """
    from app.services.report_data import load_summary

    chat = Chat(tg_chat_id=-1009160007, title="Бухгалтерия", state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    await open_period(session, chat, reason="test", at=ASKED - timedelta(days=7))
    session.add_all(
        [
            Message(
                chat_id=chat.id, tg_message_id=1, tg_user_id=ANONYMOUS_ADMIN_BOT_ID,
                transport_actor_kind=TransportActorKind.OTHER_BOT,
                business_side=BusinessSide.COMPANY, text="Выписки проведены",
                char_count=17, sent_at=DONE,
            ),
            Message(
                chat_id=chat.id, tg_message_id=2, tg_user_id=770003,
                transport_actor_kind=TransportActorKind.HUMAN_USER,
                business_side=BusinessSide.COMPANY, text="Принято",
                char_count=7, sent_at=DONE,
            ),
        ]
    )
    await session.flush()

    summary = await load_summary(session, ASKED - timedelta(hours=2), NOW)
    assert summary["outgoing"] == 2
    assert summary["unresolved"] == 1, "анонимное сообщение попало в «без автора»"


# ── Выписка ──────────────────────────────────────────────────────────────


def test_transcript_marks_anonymous_company_message():
    """В выписке видно, что автор не потерялся при разметке, а его нет."""
    from app.services.transcript import render_transcript

    anonymous = Message(
        id=1, business_side=BusinessSide.COMPANY, tg_user_id=ANONYMOUS_ADMIN_BOT_ID,
        text="Выписки с 01.09 по 15.09 проведены", has_media=False, sent_at=DONE,
    )
    nameless = Message(
        id=2, business_side=BusinessSide.COMPANY, tg_user_id=770003,
        text="Принято", has_media=False, sent_at=DONE,
    )
    rendered = render_transcript(
        [(anonymous, None), (nameless, None)], ZoneInfo("Europe/Moscow"), now=NOW
    )
    assert "🟢 компания (анонимно): Выписки" in rendered
    assert "🟢 компания: Принято" in rendered
