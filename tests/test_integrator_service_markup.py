"""Служебные ответы Битрикса и очередь «Не узнали автора» (правило стороны 5).

Служебные ответы бота Битрикса при подключении чата к порталу («Вы не
авторизованы!», «Вы уже привязаны к этому чату!») подписи сотрудника
не имеют. Проверяется:
  - маркеры узнают служебный ответ и не трогают сообщение сотрудника;
  - уход со стороны компании снимает неручную строку атрибуции;
  - «это не сотрудник» по сообщению без подписи запоминается текстом
    и снимается отменой;
  - выписка показывает всех, кто говорил, а не только клиента и компанию;
  - экран выбора автора спрашивает сначала роль, а не сыплет справочником.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.bot.callbacks import MarkupAction
from app.config import get_settings
from app.db.models import (
    Attribution,
    AttributionMethod,
    BotRole,
    BotUser,
    BotUserState,
    BusinessSide,
    Chat,
    ChatState,
    Message,
    Setting,
    Staff,
    TransportActorKind,
)
from app.services.attribution import (
    NOT_STAFF_STATE_KEY,
    NOT_STAFF_TEXTS_FIELD,
    attribute_all,
    attribute_message,
    count_not_staff,
    count_unresolved,
    mark_not_staff,
    not_staff_fingerprint,
    restore_to_queue,
    unresolved_groups,
)
from app.services.ingestion import _looks_like_service_message, resolve_business_side
from app.services.staff_roles import ROLE_MANAGER, ROLE_SPECIALIST
from app.services.transcript import load_around, render_transcript
from tests.conftest import requires_db

BASE = datetime(2026, 9, 16, 9, tzinfo=timezone.utc)
MSK = ZoneInfo("Europe/Moscow")
NOW = BASE + timedelta(hours=3)

INTEGRATOR = 990700

# Все служебные ответы сводятся к этим семи началам. Переносы строк
# и ведущее «@упоминание» сохранены намеренно: маркер должен их выдерживать.
SERVICE_TEXTS = (
    "Вы не авторизованы! Воспользуйтесь командой /auth",
    "@GroupAnonymousBot Вы авторизованы на corp.example.com как Вера Павлова",
    "@demo_vera Вы авторизованы на corp.example.com как Вера Павлова",
    "Если хотите переавторизоваться отправьте команду /auth с указанием портала",
    "Вы успешно привязаны к другому чату",
    "Вы уже привязаны к этому чату!",
    "Пересылка сообщений ИЗ этого чата В другие привязанные чаты ВКЛЮЧЕНА.\n"
    'Чтобы прекратить пересылку, используйте команду "/mute_outgoing_messages".',
    "Пересылка сообщений ИЗ этого чата В другие привязанные чаты ОСТАНОВЛЕНА.\n"
    'Чтобы возобновить пересылку, используйте команду "/mute_outgoing_messages 0".',
    "@GroupAnonymousBot \n\nID этого телеграм чата = -1001000000001\n\nВы не авторизованы",
)

# Сообщения сотрудников с ловушками: ни одно не должно попасть под маркеры.
HUMAN_TEXTS = (
    "Ирина Соколова [corp.example.com] пишет:\n\nДобрый день, передала запрос бухгалтеру",
    "Вера Павлова [corp.example.com] пишет:\n\nРабота чата восстановлена",
    "Система [corp.example.com] пишет:\n\nДоброе утро. Налоги к оплате до 28.08.",
    "Вы просили акт сверки — прикладываю",
    "Вы не прислали реквизиты, без них платёж не пройдёт",
    "Пересылка документов по этому договору идёт через ЭДО",
    "ID этой заявки уточню у специалиста",
)


def _owner() -> BotUser:
    return BotUser(
        tg_user_id=990022, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE
    )


async def _chat(session, tg_chat_id: int, title: str = "Бухгалтерия") -> Chat:
    chat = Chat(tg_chat_id=tg_chat_id, title=title, state=ChatState.TRACKED)
    session.add(chat)
    await session.flush()
    return chat


async def _integrator_message(session, chat: Chat, tg_message_id: int, text: str) -> Message:
    """Сообщение бота-интегратора, размеченное прежним правилом стороны 4 (компания)."""
    message = Message(
        chat_id=chat.id,
        tg_message_id=tg_message_id,
        tg_user_id=INTEGRATOR,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY,
        side_rule_version=4,
        text=text,
        char_count=len(text),
        sent_at=BASE + timedelta(minutes=tg_message_id),
        needs_reclassification=False,
    )
    session.add(message)
    await session.flush()
    return message


# ── Маркеры ──────────────────────────────────────────────────────────────


def test_bitrix_auth_replies_are_service_messages():
    """Все семь начал служебного диалога узнаются, в том числе с упоминанием."""
    for text in SERVICE_TEXTS:
        assert _looks_like_service_message(text), text


def test_ordinary_company_message_is_not_service():
    """Сообщение сотрудника служебным не считается.

    Ловушки здесь намеренные — «Вы не прислали реквизиты», «Пересылка
    документов», «ID этой заявки»: маркер сверяется с началом целиком,
    а не ищется где попало, иначе правило съело бы живую переписку.
    """
    for text in HUMAN_TEXTS:
        assert not _looks_like_service_message(text), text


async def test_service_reply_gets_the_integrator_side(monkeypatch):
    """Сторона служебного ответа — интегратор, а не компания."""
    monkeypatch.setattr(get_settings(), "integrator_bot_id", INTEGRATOR)
    side = await resolve_business_side(
        None, TransportActorKind.INTEGRATOR_BOT, INTEGRATOR, SERVICE_TEXTS[1]
    )
    assert side is BusinessSide.INTEGRATOR_SYSTEM
    human = await resolve_business_side(
        None, TransportActorKind.INTEGRATOR_BOT, INTEGRATOR, HUMAN_TEXTS[0]
    )
    assert human is BusinessSide.COMPANY, "обычное сообщение сотрудника ушло в служебные"


def test_fingerprint_ignores_mention_and_spacing():
    """Шаблон текста один и тот же, кому бы Битрикс ни адресовал ответ."""
    first = not_staff_fingerprint("@GroupAnonymousBot Вы авторизованы на corp.example.com")
    second = not_staff_fingerprint("@demo_vera   Вы   авторизованы\nна corp.example.com")
    assert first == second == "вы авторизованы на corp.example.com"
    assert not_staff_fingerprint(None) is None
    assert not_staff_fingerprint("   ") is None


# ── Пересчёт: уход со стороны компании снимает атрибуцию ─────────────────


@requires_db
async def test_recompute_strips_attribution_when_message_leaves_company(session, monkeypatch):
    """Ставшее служебным сообщение уходит из очереди вместе со строкой автора.

    Иначе ответ Битрикса остался бы в «Не узнали автора», уже не будучи
    сообщением компании: строка атрибуции звала бы в очередь, где выбрать
    некого.
    """
    from app.services.reprocess import recompute_sides

    monkeypatch.setattr(get_settings(), "integrator_bot_id", INTEGRATOR)

    chat = await _chat(session, -1009220001)
    service = await _integrator_message(session, chat, 1, SERVICE_TEXTS[0])
    human = await _integrator_message(session, chat, 2, HUMAN_TEXTS[0])
    await attribute_all(session)

    assert await count_unresolved(session) >= 1

    result = await recompute_sides(session)
    assert result["left_company"] >= 1

    await session.refresh(service)
    await session.refresh(human)
    assert service.business_side is BusinessSide.INTEGRATOR_SYSTEM
    assert human.business_side is BusinessSide.COMPANY, "сотрудник уехал вместе со служебными"

    left = await session.scalar(
        select(Attribution.message_id).where(Attribution.message_id == service.id)
    )
    assert left is None, "строка атрибуции у служебного сообщения осталась"
    assert result["reclassify"] == 0, "переворота клиент↔компания не было"


@requires_db
async def test_recompute_keeps_manual_attribution(session):
    """Ручное решение переживает и смену стороны: решение человека главнее."""
    from app.services.reprocess import invalidate_side_change

    chat = await _chat(session, -1009220002)
    message = await _integrator_message(session, chat, 1, SERVICE_TEXTS[2])
    session.add(
        Attribution(
            message_id=message.id,
            staff_id=None,
            method=AttributionMethod.MANUAL,
            confidence=1.0,
            parser_version=2,
        )
    )
    await session.flush()

    await invalidate_side_change(session, [], [message.id])

    survived = await session.scalar(
        select(Attribution.method).where(Attribution.message_id == message.id)
    )
    assert survived is AttributionMethod.MANUAL


# ── «Не сотрудник» без подписи ───────────────────────────────────────────


@requires_db
async def test_not_staff_without_signature_is_remembered_by_text(session):
    """Решение по сообщению без подписи запоминается текстом и переживает подключение нового чата."""
    owner = _owner()
    session.add(owner)
    first_chat = await _chat(session, -1009220003)
    await _integrator_message(session, first_chat, 1, SERVICE_TEXTS[5])
    await attribute_all(session)

    anchor = next(
        anchor for raw_name, _, anchor in await unresolved_groups(session) if raw_name is None
    )
    marked = await mark_not_staff(session, owner, anchor)
    assert marked == 1
    assert await count_not_staff(session) == 1

    stored = await session.get(Setting, NOT_STAFF_STATE_KEY)
    assert stored is not None
    assert not_staff_fingerprint(SERVICE_TEXTS[5]) in stored.value[NOT_STAFF_TEXTS_FIELD]

    # Другой чат, то же сообщение — в очередь оно уже не встаёт.
    second_chat = await _chat(session, -1009220004, "Бухгалтерия-2")
    repeat = await _integrator_message(session, second_chat, 1, SERVICE_TEXTS[5])
    await attribute_message(session, repeat)
    await session.flush()

    row = await session.get(Attribution, repeat.id)
    assert row is not None
    assert row.method is AttributionMethod.MANUAL
    assert row.staff_id is None
    assert await count_unresolved(session) == 0, "решение не удержало новое сообщение"


@requires_db
async def test_restoring_forgets_the_remembered_text(session):
    """Отмена решения убирает шаблон — иначе приём пометил бы строку обратно."""
    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220005)
    await _integrator_message(session, chat, 1, SERVICE_TEXTS[4])
    await attribute_all(session)

    anchor = next(
        anchor for raw_name, _, anchor in await unresolved_groups(session) if raw_name is None
    )
    await mark_not_staff(session, owner, anchor)
    await session.flush()

    returned = await restore_to_queue(session, owner, anchor)
    assert returned == 1
    assert await count_unresolved(session) == 1
    assert await count_not_staff(session) == 0

    stored = await session.get(Setting, NOT_STAFF_STATE_KEY)
    assert not_staff_fingerprint(SERVICE_TEXTS[4]) not in (
        stored.value.get(NOT_STAFF_TEXTS_FIELD, []) if stored else []
    )

    # И повторный проход разметки не возвращает метку исподтишка.
    await attribute_all(session)
    assert await count_unresolved(session) == 1


@requires_db
async def test_decision_covers_every_chat_with_the_same_text(session):
    """Одно нажатие накрывает все чаты, где эта же рассылка уже лежит."""
    owner = _owner()
    session.add(owner)
    for index, tg_chat_id in enumerate((-1009220006, -1009220007, -1009220008)):
        chat = await _chat(session, tg_chat_id, f"Бухгалтерия-{index}")
        await _integrator_message(session, chat, 1, SERVICE_TEXTS[3])
    await attribute_all(session)

    assert await count_unresolved(session) == 3
    anchor = next(
        anchor for raw_name, _, anchor in await unresolved_groups(session) if raw_name is None
    )
    assert await mark_not_staff(session, owner, anchor) == 3
    assert await count_unresolved(session) == 0


# ── Выписка ──────────────────────────────────────────────────────────────


def test_transcript_names_system_and_unknown_sides():
    """Значки: ⚙️ у ответа Битрикса, ⚪ у неопределённой стороны."""
    system = Message(
        id=1,
        business_side=BusinessSide.INTEGRATOR_SYSTEM,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        text="Вы уже привязаны к этому чату!",
        has_media=False,
        sent_at=BASE,
    )
    stranger = Message(
        id=2,
        business_side=BusinessSide.UNKNOWN,
        transport_actor_kind=TransportActorKind.OTHER_BOT,
        text="Ваша заявка принята",
        has_media=False,
        sent_at=BASE,
    )
    nobody = Message(
        id=3,
        business_side=BusinessSide.UNKNOWN,
        transport_actor_kind=TransportActorKind.TELEGRAM_SYSTEM,
        text="Чат переименован",
        has_media=False,
        sent_at=BASE,
    )
    client = Message(
        id=4,
        business_side=BusinessSide.CLIENT,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        text="Добрый день",
        has_media=False,
        sent_at=BASE,
    )
    rendered = render_transcript(
        [(system, None), (stranger, None), (nobody, None), (client, None)], MSK, now=NOW
    )
    assert "⚙️ система (Битрикс): Вы уже привязаны" in rendered
    assert "⚪ не определено (бот): Ваша заявка принята" in rendered
    assert "⚪ не определено: Чат переименован" in rendered
    assert "🔵 клиент: Добрый день" in rendered


@requires_db
async def test_transcript_shows_everyone_who_spoke(session):
    """Выписка показывает всех участников — кроме пустых событий Telegram.

    Фильтр по стороне не должен прятать, например, анонимного админа:
    проверяющий видит ленту такой, какой она была в чате.
    """
    chat = await _chat(session, -1009220009)
    client = Message(
        chat_id=chat.id, tg_message_id=1, tg_user_id=880001,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT, text="Подпишите акт",
        char_count=13, sent_at=BASE,
    )
    system = Message(
        chat_id=chat.id, tg_message_id=2, tg_user_id=INTEGRATOR,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.INTEGRATOR_SYSTEM,
        text="Вы не авторизованы! Воспользуйтесь командой /auth",
        char_count=49, sent_at=BASE + timedelta(minutes=1),
    )
    stranger = Message(
        chat_id=chat.id, tg_message_id=3, tg_user_id=880002,
        transport_actor_kind=TransportActorKind.OTHER_BOT,
        business_side=BusinessSide.UNKNOWN, text="/auth@its_bitrix24_bot",
        char_count=22, sent_at=BASE + timedelta(minutes=2),
    )
    joined = Message(
        chat_id=chat.id, tg_message_id=4, tg_user_id=None,
        transport_actor_kind=TransportActorKind.TELEGRAM_SYSTEM,
        business_side=BusinessSide.UNKNOWN, text=None, has_media=False,
        media_kind="service", char_count=0, sent_at=BASE + timedelta(minutes=3),
    )
    session.add_all([client, system, stranger, joined])
    await session.flush()

    rows = await load_around(
        session, chat_id=chat.id, thread_id=None, anchor_message_id=client.id,
        use_thread=False,
    )
    shown = {message.id for message, _ in rows}
    assert {client.id, system.id, stranger.id} <= shown
    assert joined.id not in shown, "пустое служебное событие Telegram попало в выписку"


# ── Экран выбора автора ──────────────────────────────────────────────────


@requires_db
async def test_pick_screen_asks_for_the_role_first(session, monkeypatch):
    """Сначала роли, потом люди роли — а не лента из всего справочника."""
    import app.bot.handlers.staff_ui as ui

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(ui, "session_scope", fake_scope)

    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220010)
    await _integrator_message(session, chat, 1, "Неразборчивая подпись без префикса")
    session.add_all(
        [
            Staff(full_name="Ирина Соколова", normalized_name="соколова ирина",
                  role=ROLE_SPECIALIST),
            Staff(full_name="Вера Павлова", normalized_name="павлова вера",
                  role=ROLE_MANAGER),
            Staff(full_name="Без Роли", normalized_name="без роли"),
        ]
    )
    await session.flush()
    await attribute_all(session)

    anchor = next(
        anchor for raw_name, _, anchor in await unresolved_groups(session) if raw_name is None
    )

    roles = _Query()
    await ui.on_markup_pick(roles, MarkupAction(action="pick", msg_id=anchor), owner)

    labels = [
        button.text
        for row in roles.message.markup.inline_keyboard
        for button in row
    ]
    assert any(label.startswith("👤 Специалисты") for label in labels)
    assert any(label.startswith("👤 Менеджеры") for label in labels)
    assert any(label.startswith("👤 Роль не определена") for label in labels)
    assert not any("Ирина Соколова" in label for label in labels), "люди показаны сразу"
    assert "🚫 Это не сотрудник" in labels
    assert "💬 Показать переписку" in labels
    # Объяснение «без подписи» живёт на экране, а не в голове нажимающего.
    assert "не разобралась строка" in roles.message.text
    assert "отсеивает сам" in roles.message.text

    # Callback data обязана помещаться в лимит Telegram.
    for row in roles.message.markup.inline_keyboard:
        for button in row:
            if button.callback_data:
                assert len(button.callback_data.encode()) <= 64, button.callback_data

    people = _Query()
    await ui.on_markup_role(people, MarkupAction(action="prsp", msg_id=anchor), owner)
    people_labels = [
        button.text for row in people.message.markup.inline_keyboard for button in row
    ]
    assert "Ирина Соколова" in people_labels
    assert "Вера Павлова" not in people_labels, "показана чужая роль"
    assert "‹ К ролям" in people_labels


@requires_db
async def test_skip_moves_to_the_next_group_without_touching_data(session, monkeypatch):
    """«Пропустить» листает очередь по кругу и ничего не решает."""
    import app.bot.handlers.staff_ui as ui

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(ui, "session_scope", fake_scope)

    owner = _owner()
    session.add(owner)
    chat = await _chat(session, -1009220011)
    await _integrator_message(session, chat, 1, "Без префикса вовсе")
    # Две группы с РАЗНЫМ числом сообщений: очередь сортируется по нему,
    # и порядок кнопок стабилен. Подпись намеренно не похожа на ФИО —
    # иначе справочник завёл бы человека сам.
    for tg_message_id in (2, 3):
        await _integrator_message(
            session,
            chat,
            tg_message_id,
            "Робот-рассылка 24 [corp.example.com] пишет:\n\nНалоги к оплате",
        )
    await attribute_all(session)

    before = await count_unresolved(session)
    assert before == 3

    groups = await unresolved_groups(session)
    anchors = [anchor for _, _, anchor in groups]
    assert len(anchors) == 2

    screen = _Query()
    await ui.on_markup_pick(screen, MarkupAction(action="pick", msg_id=anchors[0]), owner)
    skip = next(
        MarkupAction.unpack(button.callback_data)
        for row in screen.message.markup.inline_keyboard
        for button in row
        if button.callback_data
        and button.callback_data.startswith("mk:")
        and MarkupAction.unpack(button.callback_data).action == "skip"
    )
    assert skip.msg_id == anchors[1]

    nxt = _Query()
    await ui.on_markup_skip(nxt, skip, owner)
    assert nxt.message.text
    assert await count_unresolved(session) == before, "пропуск изменил данные"


class _FakeMessage:
    def __init__(self) -> None:
        self.text: str | None = None
        self.markup = None

    async def edit_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
        self.text = text
        self.markup = reply_markup


class _Query:
    def __init__(self) -> None:
        self.message = _FakeMessage()

    async def answer(self, *args, **kwargs):
        return None


@requires_db
async def test_not_staff_card_counts_the_unsigned_group(session, monkeypatch):
    """Карточка «не сотрудника» без подписи считает группу, а не ноль.

    Сравнение `raw_name = NULL` в SQL ложно всегда — запрос обязан
    обрабатывать отсутствие подписи отдельно.
    """
    import app.bot.handlers.staff_ui as ui

    from app.services.attribution import not_staff_group_size

    @asynccontextmanager
    async def fake_scope():
        yield session

    monkeypatch.setattr(ui, "session_scope", fake_scope)

    owner = _owner()
    session.add(owner)
    for index, tg_chat_id in enumerate((-1009220012, -1009220013)):
        chat = await _chat(session, tg_chat_id, f"Подключение-{index}")
        await _integrator_message(session, chat, 1, SERVICE_TEXTS[6])
    await attribute_all(session)

    anchor = next(
        anchor for raw_name, _, anchor in await unresolved_groups(session) if raw_name is None
    )
    assert await mark_not_staff(session, owner, anchor) == 2
    assert await not_staff_group_size(session, anchor) == 2

    card = _Query()
    await ui.on_markup_ignored_view(card, MarkupAction(action="iview", msg_id=anchor), owner)
    assert "Сообщений с этой подписью: 2" in card.message.text
