"""Алерты о неотвеченных обращениях.

Правила — docs/RULES.md §8, устройство — docs/ARCHITECTURE.md §8.

Два вида: no_reaction — нет никакой реакции дольше порога; no_substantive —
вопрос передан специалисту, а тот не ответил в срок. Второй вид включается
настройкой `alerts.substantive_mode` (off / on).

Срок ответа — из календаря (`calendar.response_deadline`), а не из накопленных
рабочих минут; отправка только в рабочее время. Дедупликация — по ключу
(чат, открывающее сообщение, вид) в alert_log: id эпизодов меняются при
пересборке, открывающее сообщение стабильно. По чату горит не больше одного
открытого алерта каждого вида; новые просрочки пишутся покрытыми (`covered_by_id`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramMigrateToChat
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import AlertAction
from app.config import effective_notify_group_id, get_settings
from app.db.models import (
    AlertLog,
    BotUser,
    BotUserState,
    BreachDismissal,
    BusinessSide,
    Chat,
    Classification,
    Interaction,
    InteractionState,
    Message,
)
from app.services.access import Perm, has_perm, personal_alert_recipients
from app.services.calendar import (
    CalendarHistory,
    business_seconds,
    response_deadline,
)
from app.services.dismissals import dismissed_exists as _dismissed_exists
from app.services.episodes import handoff_deadline
from app.services.tracking import currently_tracked_chats
from app.services.versioning import History
from app.services.settings_store import (
    DEFAULT_REACTION_MINUTES,
    DEFAULT_SPECIALIST_MINUTES,
    MODE_OFF,
    get_section,
    normalize_mode,
    versioned_fields,
)
from app.services.transcript import (
    WEEKDAYS,
    author_name,
    calendar_tz,
    client_label,
    client_numbers,
    escape,
    fmt_duration,
    fmt_when,
    last_company_message,
    message_at,
    snippet,
    specialist_deadline_label,
)

log = structlog.get_logger(__name__)

KIND_NO_REACTION = "no_reaction"
KIND_NO_SUBSTANTIVE = "no_substantive"

# Без предела попытки доставки заблокированному боту шли бы вечно.
MAX_DELIVERY_ATTEMPTS = 5

# Предел возраста алерта для правки его сообщения. Документированного запрета
# на правку своего сообщения нет, но на недокументированное поведение не
# полагаемся; закрытие более старого алерта видно в сводке. Тот же предел
# снимает алерт с правила «один открытый алерт вида на чат»: сообщение,
# которое уже не зачеркнуть, не держит чат.
STRIKE_MAX_AGE = timedelta(hours=48)

KIND_HEADERS = {
    KIND_NO_REACTION: "🔴 <b>1 · Нет реакции</b>",
    KIND_NO_SUBSTANTIVE: "🟠 <b>2 · Нет ответа специалиста</b>",
}


def duplicate_burst_ids(
    interactions, threshold_minutes: int, anchor_ids: set[int] | None = None
) -> set[int]:
    """id обращений-дублей внутри одной неотреагированной очереди сообщений клиента.

    Каждая просьба сохраняет свой срок в движке, но будить владельца несколькими
    строками об одном молчании незачем: обращения одного чата и ветки, открытые
    в пределах порога первой реакции от самого раннего, схлопываются в него
    (у него самый ранний срок).

    `anchor_ids` — обращения, которым разрешено быть анкером (сами поднимут алерт
    или алерт по ним уже горит); анкер, который никого не будит, гасил бы пачку
    в тишину. None — анкером может стать любое.
    """
    window = timedelta(minutes=max(int(threshold_minutes), 0))
    suppressed: set[int] = set()
    groups: dict[tuple[int, int | None], list] = {}
    for interaction in interactions:
        groups.setdefault((interaction.chat_id, interaction.thread_id), []).append(interaction)
    for items in groups.values():
        items.sort(key=lambda item: (item.opened_at, item.id))
        anchor = None
        for item in items:
            if anchor is not None and item.opened_at - anchor.opened_at <= window:
                suppressed.add(item.id)
                continue
            # Окно прошлого анкера кончилось: очередь начинается заново — с того, кто разбудит.
            anchor = item if anchor_ids is None or item.id in anchor_ids else None
    return suppressed


def _fmt_calendar_age(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return f"{days} дн {hours} ч"
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    return f"{minutes} мин"


def _schedule_hint(calendar_cfg: dict[str, Any]) -> str:
    days = sorted(calendar_cfg.get("weekdays") or [1, 2, 3, 4, 5])
    span = f"{WEEKDAYS[days[0] - 1]}–{WEEKDAYS[days[-1] - 1]}"
    return f"{calendar_cfg.get('start', '?')}–{calendar_cfg.get('end', '?')}, {span}"


def _context_kb(chat_id: int, message_id: int):
    builder = InlineKeyboardBuilder()
    # ctxd — выписка с решением «снять нарушение»; сотруднику без права
    # сводного отчёта экран открывается без кнопки решения.
    builder.button(
        text="💬 Показать переписку",
        callback_data=AlertAction(action="ctxd", chat_id=chat_id, msg_id=message_id).pack(),
    )
    builder.adjust(1)
    return builder.as_markup()


class _GroupTarget(NamedTuple):
    """Группа уведомлений в списке адресатов.

    Поле названо tg_user_id, чтобы доставка работала с ней как с обычным
    получателем; отрицательный id в alert_log.recipients — группа.
    """

    tg_user_id: int


async def _alert_targets(session: AsyncSession, alert_cfg: dict[str, Any]) -> list:
    """Личка каждого владельца плюс, если включено, группа уведомлений.

    Группа — дополнение к личкам, а не замена.
    """
    targets = list(await personal_alert_recipients(session))
    notify_group_id = effective_notify_group_id()
    if bool(alert_cfg.get("to_group")) and notify_group_id is not None:
        targets.append(_GroupTarget(tg_user_id=notify_group_id))
    return targets


async def _self_target(session: AsyncSession, interaction) -> "BotUser | None":
    """Сотрудник, для которого этот алерт — «свой».

    Определён только у «нет ответа специалиста»: менеджер, который передал
    вопрос (handoff_staff_id). У «нет реакции» своего нет: закрепления чатов
    за менеджерами не существует.
    """
    if interaction.handoff_staff_id is None:
        return None
    user = await session.scalar(
        select(BotUser)
        .where(BotUser.staff_id == interaction.handoff_staff_id)
        .where(BotUser.state == BotUserState.ACTIVE)
        .limit(1)
    )
    if user is None or not has_perm(user, Perm.ALERT_RECEIVE_SELF):
        return None
    # Флаг «уведомления в личку» действует и на «свой» алерт.
    if not user.notify_personal:
        return None
    return user


async def _alert_context(session: AsyncSession, interaction, chat_is_forum) -> tuple:
    """Реакция, последнее от клиента и последнее от компании — для текста алерта.

    Одна функция на первую отправку и на досылку.
    """
    reaction = None
    if interaction.first_reaction_at is not None:
        # Сначала по id (по времени два сообщения в одну секунду неразличимы);
        # поиск по времени — запасной для обращений без id реакции.
        reaction_message = None
        if interaction.first_reaction_message_id is not None:
            reaction_message = await session.get(Message, interaction.first_reaction_message_id)
        if reaction_message is None:
            reaction_message = await message_at(
                session,
                interaction.chat_id,
                BusinessSide.COMPANY,
                interaction.first_reaction_at,
            )
        if reaction_message is not None:
            reaction = (
                reaction_message,
                await author_name(session, reaction_message.id),
            )

    last_client = None
    if interaction.client_messages > 1:
        last_client = await message_at(
            session,
            interaction.chat_id,
            BusinessSide.CLIENT,
            interaction.last_client_at,
        )

    last_company = await last_company_message(
        session,
        interaction.chat_id,
        interaction.thread_id,
        use_thread=bool(chat_is_forum),
    )
    return reaction, last_client, last_company


async def _deliver(
    bot, recipients, text: str, markup, plain_ids: frozenset[int] = frozenset()
) -> tuple[list[int], str | None, dict[str, int]]:
    """Разослать алерт. Возвращает (кому дошло, последняя ошибка, {"<tg_id>": message_id}).

    Ошибка на одном адресате не прерывает рассылку и не считается доставкой.
    id сообщений нужны для зачёркивания; ключи строковые, как их хранит JSONB.
    """
    delivered: list[int] = []
    message_ids: dict[str, int] = {}
    error: str | None = None
    for user in recipients:
        # В группу — без кнопок: бот там только публикует. Сотруднику из
        # `plain_ids` — тоже без кнопки: права на выписку у него нет.
        target_markup = (
            markup if user.tg_user_id > 0 and user.tg_user_id not in plain_ids else None
        )
        try:
            sent = await bot.send_message(
                user.tg_user_id, text, parse_mode="HTML", reply_markup=target_markup
            )
            delivered.append(user.tg_user_id)
            if getattr(sent, "message_id", None) is not None:
                message_ids[str(user.tg_user_id)] = sent.message_id
        except TelegramMigrateToChat as exc:
            # Группа мигрировала в супергруппу: досылаем на новый номер
            # из ошибки и запоминаем переезд.
            new_chat_id = exc.migrate_to_chat_id
            try:
                sent = await bot.send_message(
                    new_chat_id, text, parse_mode="HTML", reply_markup=None
                )
                # Доставленным считается исходный адресат (по нему учёт повторов),
                # а id сообщения запоминается под новым номером — его и править.
                delivered.append(user.tg_user_id)
                if getattr(sent, "message_id", None) is not None:
                    message_ids[str(new_chat_id)] = sent.message_id
                from app.db.base import session_scope
                from app.services.notify_group import (
                    MIGRATION_NOTICE,
                    record_migration,
                )

                async with session_scope() as session:
                    first_time = await record_migration(session, new_chat_id)
                if first_time:
                    for owner in recipients:
                        if owner.tg_user_id > 0:
                            try:
                                await bot.send_message(
                                    owner.tg_user_id,
                                    MIGRATION_NOTICE,
                                    parse_mode="HTML",
                                )
                            except Exception:
                                log.info(
                                    "alerts.migration_notice_failed",
                                    tg_user_id=owner.tg_user_id,
                                    exc_info=True,
                                )
            except Exception as inner:  # noqa: BLE001 — новый номер тоже не принял
                error = f"{type(inner).__name__}: {inner}"[:300]
                log.exception("alerts.migrate_resend_failed", new_chat_id=new_chat_id)
        except Exception as exc:  # noqa: BLE001 — один недоставленный не роняет тик
            error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("alerts.send_failed", tg_user_id=user.tg_user_id)
    return delivered, error, message_ids


async def retry_undelivered(session: AsyncSession, bot: Bot) -> int:
    """Повторить алерты, которые записались, но не дошли.

    Правила: вне рабочего времени не досылаем; неактуальное (ответили,
    сняли, чат не в анализе, вид выключен) закрывается без отправки;
    повтор — только тем, кому в прошлый раз не дошло.
    """
    alert_cfg = await get_section(session, "alerts")
    calendar_cfg = await get_section(session, "work_calendar")
    calendar = CalendarHistory(calendar_cfg)
    alerts_history = History(alert_cfg, versioned_fields("alerts"))
    now = datetime.now(timezone.utc)

    quiet = bool(alert_cfg.get("respect_quiet_hours", True)) and (
        business_seconds(now - timedelta(minutes=1), now, calendar_cfg) == 0
    )
    if quiet:
        return 0

    stale = (
        await session.scalars(
            select(AlertLog)
            .where(AlertLog.delivered.is_(False))
            .where(AlertLog.shadow.is_(False))
            # Покрытая запись сообщением не была и досылке не подлежит
            # (она и так пишется delivered — условие явное намеренно).
            .where(AlertLog.covered_by_id.is_(None))
            .where(AlertLog.attempts < MAX_DELIVERY_ATTEMPTS)
            .order_by(AlertLog.id)
            .limit(20)
        )
    ).all()
    if not stale:
        return 0

    # Досылка смотрит на те же выключатели, что и первая отправка.
    alerts_enabled = bool(alert_cfg.get("enabled"))
    substantive_mode = normalize_mode(alert_cfg.get("substantive_mode"))

    recipients = await _alert_targets(session, alert_cfg)
    recovered = 0

    for entry in stale:
        row = (
            await session.execute(
                select(Interaction, Chat.title, Chat.is_forum, Message)
                .join(Chat, Chat.id == Interaction.chat_id)
                .join(Message, Message.id == Interaction.opened_by_message_id)
                .where(Interaction.chat_id == entry.chat_id)
                .where(Interaction.opened_by_message_id == entry.opened_by_message_id)
                .where(Interaction.state.in_([InteractionState.OPEN, InteractionState.REACTED]))
                .where(Interaction.chat_id.in_(currently_tracked_chats()))
                .where(~_dismissed_exists())
            )
        ).first()
        interaction = row[0] if row is not None else None
        # Актуальность — по тем же признакам, по которым алерт рождается.
        stale_reason = None
        if interaction is None:
            stale_reason = "неактуально: ответили, сняли или чат снят с анализа"
        elif not alerts_enabled:
            stale_reason = "неактуально: алерты выключены"
        elif entry.kind == KIND_NO_REACTION and interaction.state is not InteractionState.OPEN:
            stale_reason = "неактуально: реакция уже была"
        elif entry.kind == KIND_NO_SUBSTANTIVE and (
            substantive_mode == MODE_OFF
            or interaction.handoff_at is None
            or interaction.substantive_at is not None
        ):
            stale_reason = "неактуально: специалист вышел на связь или ступень выключена"
        if stale_reason is not None:
            entry.delivered = True
            entry.last_error = stale_reason
            log.info(
                "alerts.retry_dropped", kind=entry.kind, chat_id=entry.chat_id, reason=stale_reason
            )
            continue
        if not recipients:
            return recovered

        interaction, chat_title, chat_is_forum, opener = row
        case_rules = alerts_history.at(interaction.opened_at)
        if entry.kind == KIND_NO_REACTION:
            limit_minutes = int(case_rules.get("threshold_minutes") or DEFAULT_REACTION_MINUTES)
        else:
            limit_minutes = int(
                case_rules.get("substantive_threshold_minutes") or DEFAULT_SPECIALIST_MINUTES
            )
        case_cfg = calendar.at(interaction.opened_at)
        if entry.kind == KIND_NO_SUBSTANTIVE:
            deadline = handoff_deadline(
                interaction.handoff_at, limit_minutes * 60, case_cfg
            )
        else:
            deadline = response_deadline(
                interaction.opened_at, limit_minutes * 60, case_cfg
            )
        reaction, last_client, last_company = await _alert_context(
            session, interaction, chat_is_forum
        )
        text = build_alert_text(
            kind=entry.kind,
            chat_title=chat_title,
            opener=opener,
            interaction=interaction,
            last_client=last_client,
            client_numbers=await client_numbers(session, interaction.chat_id),
            reaction=reaction,
            last_company=last_company,
            deadline=deadline or now,
            calendar_age=int((now - interaction.opened_at).total_seconds()),
            limit_minutes=limit_minutes,
            calendar_cfg=case_cfg,
            now=now,
        )
        markup = _context_kb(entry.chat_id, entry.opened_by_message_id)

        # Адресаты — те же, что при первой отправке, включая «свой» у второго вида.
        alert_recipients = list(recipients)
        plain_ids: frozenset[int] = frozenset()
        if entry.kind == KIND_NO_SUBSTANTIVE:
            personal = await _self_target(session, interaction)
            if personal is not None and personal.tg_user_id not in {
                user.tg_user_id for user in alert_recipients
            }:
                alert_recipients.append(personal)
                plain_ids = frozenset({personal.tg_user_id})

        already = set(entry.recipients or [])
        pending = [user for user in alert_recipients if user.tg_user_id not in already]
        if not pending:
            entry.delivered = True
            continue

        delivered_to, error, sent_ids = await _deliver(bot, pending, text, markup, plain_ids)

        entry.attempts += 1
        entry.last_error = error
        if delivered_to:
            entry.recipients = sorted(already | set(delivered_to))
            entry.message_ids = {**(entry.message_ids or {}), **sent_ids}
            entry.sent_text = text
            # Подмножество id, а не сравнение длин: состав адресатов мог смениться.
            expected = {user.tg_user_id for user in alert_recipients}
            entry.delivered = expected.issubset(entry.recipients)
            recovered += 1
            log.info("alerts.delivery_recovered", kind=entry.kind, chat=chat_title)

    return recovered


def build_alert_text(
    *,
    kind: str,
    chat_title: str | None,
    opener: Message,
    interaction: Interaction,
    last_client: Message | None,
    reaction: tuple[Message, str | None] | None,
    last_company: tuple[Message, str | None] | None,
    deadline: datetime,
    calendar_age: int,
    limit_minutes: int,
    calendar_cfg: dict[str, Any],
    now: datetime,
    client_numbers: dict[int, int] | None = None,
) -> str:
    """Текст алерта: срок и просрочка, чат, что написал клиент и что ответила компания.

    `client_numbers` различает людей со стороны клиента («Клиент 1», «от клиента 2»):
    иначе разговор двух сотрудников клиента читается как клиент, отвечающий сам себе.
    """
    tz = calendar_tz(calendar_cfg)
    opener_label = client_label(opener, client_numbers)
    overdue = int((now - deadline).total_seconds())
    lines = [
        KIND_HEADERS.get(kind, kind),
        f"Просрочено на <b>{fmt_duration(max(overdue, 0))}</b> — "
        f"ответить надо было до {fmt_when(deadline, tz, now)} "
        + (
            f"(срок: {specialist_deadline_label(limit_minutes)})"
            if kind == KIND_NO_SUBSTANTIVE
            else f"(порог {limit_minutes} мин)"
        ),
        "",
        f"Чат: <b>{escape(chat_title or '?')}</b>",
        "",
        f"👤 <b>{opener_label.capitalize()}</b> — {fmt_when(opener.sent_at, tz, now)}",
        f"«{snippet(opener)}»",
    ]

    if interaction.client_messages > 1:
        last_label = (
            client_label(last_client, client_numbers) if last_client is not None else "клиент"
        )
        lines.append("")
        lines.append(
            f"👤 <b>Последнее от {last_label.replace('клиент', 'клиента', 1)}</b> — "
            f"{fmt_when(interaction.last_client_at, tz, now)} "
            f"(всего сообщений: {interaction.client_messages})"
        )
        if last_client is not None:
            lines.append(f"«{snippet(last_client)}»")
    lines.append("")

    if kind == KIND_NO_SUBSTANTIVE and reaction is not None:
        message, author = reaction
        who = f", {escape(author)}" if author else ""
        lines.append(f"🏢 <b>Компания ответила</b> — {fmt_when(message.sent_at, tz, now)}{who}")
        lines.append(f"«{snippet(message)}»")
        lines.append(
            "<i>Это зачлось реакцией; ответа специалиста после передачи ещё не было.</i>"
        )

        if last_company is not None and last_company[0].id != message.id:
            latest, latest_author = last_company
            who = f", {escape(latest_author)}" if latest_author else ""
            lines.append("")
            lines.append(
                f"🏢 <b>Последнее от компании</b> — "
                f"{fmt_when(latest.sent_at, tz, now)}{who}"
            )
            lines.append(f"«{snippet(latest)}»")
    elif kind == KIND_NO_SUBSTANTIVE and interaction.first_reaction_at is not None:
        # Реакция была, но само сообщение не нашлось — «не отвечала» писать нельзя.
        lines.append(
            f"🏢 <b>Компания отреагировала</b> — "
            f"{fmt_when(interaction.first_reaction_at, tz, now)}; ответа "
            "специалиста после передачи ещё не было."
        )
    else:
        lines.append("🏢 <b>Компания не отвечала</b> с момента обращения.")
        if last_company is not None and last_company[0].sent_at < opener.sent_at:
            message, author = last_company
            who = f", {escape(author)}" if author else ""
            lines.append(
                f"<i>Последнее сообщение компании было до обращения — "
                f"{fmt_when(message.sent_at, tz, now)}{who}.</i>"
            )

    lines.append("")
    lines.append(
        f"<i>Календарно ждёт {_fmt_calendar_age(calendar_age)}. "
        f"График: {_schedule_hint(calendar_cfg)}.</i>"
    )
    return "\n".join(lines)


async def _awaiting_verdict(session: AsyncSession, interaction) -> bool:
    """В обращении есть сообщение клиента, возвращённое в очередь без вердикта.

    Только `needs_reclassification` (пересчёт стороны, правка, reclassify.py):
    вердикт был и скоро будет снова. Свежее сообщение без вердикта алерт
    не придерживает («нет вердикта = будим»). Строка с ошибкой здесь считается
    разобранной — в отличие от очереди воркера, где она означает повтор:
    очередь ещё ждёт разметки, а алерт ждать перестал. После исчерпания попыток
    воркер гасит флаг вместе с техническим вердиктом.
    """
    classified = (
        select(Classification.message_id)
        .where(Classification.model.in_(get_settings().ai_accepted_models))
        .scalar_subquery()
    )
    pending = await session.scalar(
        select(func.count(Message.id))
        .where(Message.chat_id == interaction.chat_id)
        .where(Message.business_side == BusinessSide.CLIENT)
        .where(Message.sent_at >= interaction.opened_at)
        .where(Message.sent_at <= (interaction.last_client_at or interaction.opened_at))
        .where(Message.text.isnot(None))
        .where(Message.needs_reclassification.is_(True))
        .where(Message.id.notin_(classified))
    )
    return bool(pending)


def _first_layer_overdue(
    interaction: Interaction,
    alerts_history: History,
    calendar: CalendarHistory,
    now: datetime,
) -> bool:
    """Первый слой этого обращения просрочен — неважно, ответили ли потом.

    Пороги и график — на момент обращения, как в самом проходе алертов.
    """
    limit = int(
        alerts_history.at(interaction.opened_at).get("threshold_minutes")
        or DEFAULT_REACTION_MINUTES
    )
    deadline = response_deadline(
        interaction.opened_at, limit * 60, calendar.at(interaction.opened_at)
    )
    if deadline is None:
        return False
    return (interaction.first_reaction_at or now) > deadline


async def _burst_peers(
    session: AsyncSession, candidates: list, window_minutes: int
) -> list[Interaction]:
    """Соседи по очереди клиента — включая уже закрытые обращения.

    Анкер очереди мог закрыться и выпасть из кандидатов, но для схлопывания
    он нужен. «Ответ не требуется» и снятые решением анкерами не становятся:
    они никого не будят.
    """
    chats = {row[0].chat_id for row in candidates}
    if not chats:
        return []
    since = min(row[0].opened_at for row in candidates) - timedelta(
        minutes=max(window_minutes, 0)
    )
    rows = await session.execute(
        select(Interaction)
        .where(Interaction.chat_id.in_(chats))
        .where(Interaction.opened_at >= since)
        .where(Interaction.state != InteractionState.NO_RESPONSE_NEEDED)
        .where(~_dismissed_exists())
    )
    return list(rows.scalars())


def _open_alert_filters(now: datetime) -> tuple:
    """Условия «алерт горит»: ушёл получателям, не покрыт и ещё не закрыт."""
    return (
        AlertLog.shadow.is_(False),
        AlertLog.covered_by_id.is_(None),
        AlertLog.struck_at.is_(None),
        AlertLog.recipients != [],
        AlertLog.sent_at >= now - STRIKE_MAX_AGE,
    )


async def _notified_first_layer(session: AsyncSession, now: datetime) -> set[int]:
    """Открывающие сообщения обращений, по которым горит алерт первого слоя.

    Такое обращение остаётся анкером пачки и после ответа (пусть позднего):
    о молчании этой очереди сообщение уже ушло.
    """
    rows = await session.execute(
        select(AlertLog.opened_by_message_id)
        .where(AlertLog.kind == KIND_NO_REACTION)
        .where(*_open_alert_filters(now))
    )
    return set(rows.scalars())


async def _open_layer_alerts(
    session: AsyncSession, now: datetime
) -> dict[tuple[int, str], int]:
    """Горящие алерты: (чат, вид) → id открытого алерта этого слоя.

    Открытый — доставленный, не покрытый и без `struck_at` (закрытие отмечает
    `strike_closed_alerts`). Недоставленный открытым не считается: его доберёт
    `retry_undelivered`, а новая просрочка получит своё сообщение.
    """
    rows = await session.execute(
        select(AlertLog.chat_id, AlertLog.kind, AlertLog.id)
        .where(*_open_alert_filters(now))
        .order_by(AlertLog.id)
    )
    # Порядок по id: если открытых по слою окажется два, покрывающим станет самый свежий.
    return {(chat_id, kind): alert_id for chat_id, kind, alert_id in rows}


async def _log_alert(
    session: AsyncSession,
    *,
    interaction,
    kind: str,
    recipients: list[int],
    covered_by_id: int | None,
) -> int | None:
    """Записать событие в журнал. None — по этому обращению уже оповещали.

    Дедупликация атомарной вставкой (конфликт — молча мимо). Запись означает
    «событие зафиксировано», а не «доставлено» — доставку отмечает delivered.
    """
    inserted = await session.execute(
        pg_insert(AlertLog)
        .values(
            chat_id=interaction.chat_id,
            opened_by_message_id=interaction.opened_by_message_id,
            kind=kind,
            recipients=recipients,
            shadow=False,
            # Покрытому доставлять нечего: его сообщение ушло по покрывающему.
            delivered=covered_by_id is not None,
            covered_by_id=covered_by_id,
        )
        .on_conflict_do_nothing(constraint="uq_alert_once")
        .returning(AlertLog.id)
    )
    return inserted.scalar_one_or_none()


async def process_alerts(session: AsyncSession, bot: Bot) -> dict[str, int]:
    """Тик алертов: шлёт алерты по просроченным обращениям в рабочее время.

    Возвращает число отправленных (`sent`) и покрытых уже горящим алертом (`covered`).
    """
    alert_cfg = await get_section(session, "alerts")
    if not alert_cfg.get("enabled"):
        return {"sent": 0, "covered": 0}

    calendar_cfg = await get_section(session, "work_calendar")
    # Два графика: «можно ли писать сейчас» — по действующему (окно тишины),
    # «просрочено ли обращение» — по действовавшему в момент обращения.
    calendar = CalendarHistory(calendar_cfg)
    now = datetime.now(timezone.utc)

    # Окно тишины: вне рабочего времени не отправляем, алерт уйдёт первым тиком
    # рабочего дня. Проверка: последняя минута содержит рабочие секунды.
    quiet = bool(alert_cfg.get("respect_quiet_hours", True)) and (
        business_seconds(now - timedelta(minutes=1), now, calendar_cfg) == 0
    )

    # Пороги — на момент обращения, как у эпизода, считающего просрочку.
    alerts_history = History(alert_cfg, versioned_fields("alerts"))

    substantive_mode = normalize_mode(alert_cfg.get("substantive_mode"))

    if quiet:
        return {"sent": 0, "covered": 0}

    # Получатели — по праву «получать алерты», а не по названию роли,
    # плюс группа уведомлений, если включена.
    recipients = await _alert_targets(session, alert_cfg)
    if not recipients:
        log.warning("alerts.no_recipients")
        return {"sent": 0, "covered": 0}

    candidates = (
        await session.execute(
            select(Interaction, Chat.title, Chat.is_forum, Message)
            .join(Chat, Chat.id == Interaction.chat_id)
            .join(Message, Message.id == Interaction.opened_by_message_id)
            .where(Interaction.state.in_([InteractionState.OPEN, InteractionState.REACTED]))
            .where(Interaction.chat_id.in_(currently_tracked_chats()))
            .where(~_dismissed_exists())
            # Порядок обращений определяет, какое станет видимым алертом, а какое
            # будет покрыто: видимым должно быть самое раннее (самый ранний срок).
            .order_by(Interaction.opened_at, Interaction.id)
        )
    ).all()

    # Схлопывание очереди просьб клиента: применяется ко всем просроченным
    # обращениям первого слоя, независимо от состояния. Анкер — только тот,
    # кто кого-то будит: сам в кандидатах на отправку или алерт по нему уже
    # горит; отвеченное позже срока пачку не гасит. Второй слой считается
    # по каждой передаче отдельно.
    burst_window = int(alert_cfg.get("threshold_minutes") or DEFAULT_REACTION_MINUTES)
    burst_peers = [
        row
        for row in await _burst_peers(session, candidates, burst_window)
        if _first_layer_overdue(row, alerts_history, calendar, now)
    ]
    notified_openers = await _notified_first_layer(session, now)
    candidate_ids = {row[0].id for row in candidates}
    burst_duplicates = duplicate_burst_ids(
        burst_peers,
        burst_window,
        anchor_ids={
            peer.id
            for peer in burst_peers
            if peer.id in candidate_ids
            or peer.opened_by_message_id in notified_openers
        },
    )

    # Что уже горит по каждому чату и слою: вторая просрочка того же слоя
    # сообщения не поднимает, пока алерт не закрыт.
    open_layer = await _open_layer_alerts(session, now)

    sent = 0
    covered = 0
    for interaction, chat_title, chat_is_forum, opener in candidates:
        case_rules = alerts_history.at(interaction.opened_at)
        if interaction.state is InteractionState.OPEN:
            if interaction.id in burst_duplicates:
                continue
            kind = KIND_NO_REACTION
            limit_minutes = int(case_rules.get("threshold_minutes") or DEFAULT_REACTION_MINUTES)
        elif interaction.state is InteractionState.REACTED and substantive_mode != MODE_OFF:
            # Второй слой — только там, где была передача специалисту.
            if interaction.handoff_at is None:
                continue
            # Специалист уже вышел на связь (пусть встречным вопросом) — срок выполнен.
            if interaction.substantive_at is not None:
                continue
            kind = KIND_NO_SUBSTANTIVE
            limit_minutes = int(
                case_rules.get("substantive_threshold_minutes") or DEFAULT_SPECIALIST_MINUTES
            )
        else:
            continue

        case_cfg = calendar.at(interaction.opened_at)
        if kind == KIND_NO_SUBSTANTIVE:
            # Срок специалиста считается от передачи, а не от обращения клиента.
            deadline = handoff_deadline(
                interaction.handoff_at, limit_minutes * 60, case_cfg
            )
        else:
            # Срок, а не накопленные минуты: обращение в конце дня получает
            # полный порог с открытия следующего рабочего дня.
            deadline = response_deadline(
                interaction.opened_at, limit_minutes * 60, case_cfg
            )
        if deadline is None or now <= deadline:
            continue

        # Текстовое сообщение клиента ещё ждёт повторного вердикта — будить рано:
        # «спасибо» может закрыть обращение само. Медиа без подписи вердикта не ждёт.
        if await _awaiting_verdict(session, interaction):
            log.info("alerts.awaiting_verdict", kind=kind, chat=chat_title)
            continue

        # Не больше одного открытого алерта слоя на чат. Проверка — после всех
        # условий срабатывания.
        covering_id = open_layer.get((interaction.chat_id, kind))
        if covering_id is not None:
            # Сообщения нет, запись есть: сводка и метрики видят просрочку,
            # а закрытие покрывающего алерта засчитывает её оповещённой.
            covered_id = await _log_alert(
                session,
                interaction=interaction,
                kind=kind,
                recipients=[],
                covered_by_id=covering_id,
            )
            if covered_id is not None:
                covered += 1
                log.info(
                    "alerts.covered",
                    kind=kind,
                    chat=chat_title,
                    covered_by=covering_id,
                )
            continue

        # «Свой» алерт: менеджер, передавший вопрос, получает «нет ответа
        # специалиста» в дополнение к владельцам.
        alert_recipients = list(recipients)
        plain_ids: frozenset[int] = frozenset()
        if kind == KIND_NO_SUBSTANTIVE:
            personal = await _self_target(session, interaction)
            if personal is not None and personal.tg_user_id not in {
                user.tg_user_id for user in alert_recipients
            }:
                alert_recipients.append(personal)
                plain_ids = frozenset({personal.tg_user_id})

        alert_id = await _log_alert(
            session,
            interaction=interaction,
            kind=kind,
            recipients=[user.tg_user_id for user in alert_recipients],
            covered_by_id=None,
        )
        if alert_id is None:
            continue  # уже оповещали

        reaction, last_client, last_company = await _alert_context(
            session, interaction, chat_is_forum
        )

        text = build_alert_text(
            kind=kind,
            chat_title=chat_title,
            opener=opener,
            interaction=interaction,
            last_client=last_client,
            client_numbers=await client_numbers(session, interaction.chat_id),
            reaction=reaction,
            last_company=last_company,
            deadline=deadline,
            calendar_age=int((now - interaction.opened_at).total_seconds()),
            limit_minutes=limit_minutes,
            calendar_cfg=case_cfg,
            now=now,
        )
        markup = _context_kb(interaction.chat_id, interaction.opened_by_message_id)

        delivered_to, error, sent_ids = await _deliver(
            bot, alert_recipients, text, markup, plain_ids
        )
        await session.execute(
            update(AlertLog)
            .where(AlertLog.id == alert_id)
            .values(
                # «Доставлено» — когда дошло всем (подмножество id, а не сравнение
                # длин); иначе retry_undelivered не дослал бы остальным.
                delivered={u.tg_user_id for u in alert_recipients}.issubset(delivered_to),
                attempts=AlertLog.attempts + 1,
                last_error=error,
                recipients=delivered_to,
                message_ids=sent_ids,
                sent_text=text,
            )
        )
        if not delivered_to:
            log.warning("alerts.delivery_failed", kind=kind, chat=chat_title, error=error)
            continue
        sent += 1
        # Теперь алерт горит по чату: следующая просрочка того же слоя
        # (хоть в этом же тике) покрывается им.
        open_layer[(interaction.chat_id, kind)] = alert_id
        log.info(
            "alerts.sent",
            kind=kind,
            chat=chat_title,
            overdue_seconds=int((now - deadline).total_seconds()),
        )

    return {"sent": sent, "covered": covered}


# Заголовок закрытого алерта: цвет меняется, номер вида остаётся.
_CLOSED_HEADERS = {
    "closed": {
        KIND_NO_REACTION: "🟢 <b>1 · Нет реакции — отработано</b>",
        KIND_NO_SUBSTANTIVE: "🟢 <b>2 · Нет ответа специалиста — отработано</b>",
    },
    "dismissed": {
        KIND_NO_REACTION: "✋ <b>1 · Нет реакции — закрыто вручную</b>",
        KIND_NO_SUBSTANTIVE: "✋ <b>2 · Нет ответа специалиста — закрыто вручную</b>",
    },
    "no_need": {
        KIND_NO_REACTION: "🤖 <b>1 · Нет реакции — ответа не требовалось</b>",
        KIND_NO_SUBSTANTIVE: "🤖 <b>2 · Нет ответа специалиста — ответа не требовалось</b>",
    },
}

# Исходы, при которых сообщение правится. no_answer не входит намеренно:
# худший исход не прячем под зачёркиванием. gone — писать нечего.
STRIKEABLE = frozenset(_CLOSED_HEADERS)


def build_struck_text(sent_text: str, kind: str, outcome: str, tail: str) -> str:
    """Текст закрытого алерта: новый заголовок, тело зачёркнуто, приписка.

    Тело — из сохранённого текста, а не пересобранное: состояние обращения уже другое.
    """
    header, _, body = sent_text.partition("\n")
    new_header = _CLOSED_HEADERS[outcome].get(kind, header)
    body = body.strip("\n")
    parts = [new_header]
    if body:
        # <s> вокруг всего тела: вложенные <b>/<i> Telegram допускает.
        parts.append(f"<s>{body}</s>")
    parts.append(f"<i>{tail}</i>")
    text = "\n\n".join(parts)
    # Предел Telegram — 4096 символов: при переполнении тело отбрасывается.
    if len(text) > 4000:
        text = f"{new_header}\n\n<i>{tail}</i>"
    return text


async def strike_closed_alerts(session: AsyncSession, bot: Bot, limit: int = 20) -> int:
    """Отметить в самом сообщении алерта, что кейс закрыт. Возвращает сколько.

    Правим один раз на алерт: `struck_at` ставится после попытки, даже если
    Telegram отказал (бота удалили, сообщение стёрто). Отказ — штатный исход,
    тик он не роняет; закрытие всё равно видно в сводке.
    """
    from app.services.alert_digest import alert_outcome
    from app.services.dismissals import is_dismissed

    entries = (
        await session.scalars(
            select(AlertLog)
            # Открытые обращения исключаются до лимита, иначе горящие алерты
            # занимали бы окно целиком. Пропавшее обращение — тоже кандидат
            # (ему ставится отметка без правки).
            .outerjoin(
                Interaction,
                (Interaction.chat_id == AlertLog.chat_id)
                & (Interaction.opened_by_message_id == AlertLog.opened_by_message_id),
            )
            .where(
                or_(
                    Interaction.id.is_(None),
                    Interaction.state.notin_([InteractionState.OPEN, InteractionState.REACTED]),
                    # Снятое вручную обращение открыто в движке, но алерт по нему закрыт.
                    select(BreachDismissal.id)
                    .where(BreachDismissal.chat_id == AlertLog.chat_id)
                    .where(BreachDismissal.opened_by_message_id == AlertLog.opened_by_message_id)
                    .exists(),
                )
            )
            .where(AlertLog.struck_at.is_(None))
            .where(AlertLog.shadow.is_(False))
            # Пустой message_ids — алерт не доставлялся: править нечего.
            .where(AlertLog.message_ids != {})
            .order_by(AlertLog.id)
            .limit(limit)
        )
    ).all()
    if not entries:
        return 0

    now = datetime.now(timezone.utc)
    struck = 0

    for entry in entries:
        if now - entry.sent_at > STRIKE_MAX_AGE:
            # Старше предела — не пробуем, но отмечаем, чтобы запись не занимала окно.
            entry.struck_at = now
            log.info("alerts.strike_too_old", chat_id=entry.chat_id, kind=entry.kind)
            continue
        interaction = await session.scalar(
            select(Interaction)
            .where(Interaction.chat_id == entry.chat_id)
            .where(Interaction.opened_by_message_id == entry.opened_by_message_id)
            .limit(1)
        )
        # Важно: отметку ставим и там, где правки не будет: выборка идёт по
        # struck_at IS NULL с лимитом. Открытым (waiting) и алертам с ждущими
        # покрытыми отметка не ставится — они вернутся сюда после закрытия.
        if interaction is None:
            if await _covered_still_waiting(session, entry.id):
                # Важно: пока ждёт хоть одна покрытая просрочка, алерт не закрывать:
                # с `struck_at` он перестаёт быть открытым, а покрытая своего
                # сообщения уже не получит (ключ в alert_log занят).
                continue
            entry.struck_at = now
            continue
        dismissed = await is_dismissed(
            session, entry.chat_id, entry.opened_by_message_id
        )
        outcome = alert_outcome(entry.kind, interaction, dismissed)
        if outcome == "waiting":
            continue  # ещё горит — вернёмся, когда закроется
        if await _covered_still_waiting(session, entry.id):
            # Алерт покрыл просрочку того же слоя, которая ещё открыта, — закрывать рано.
            continue
        if outcome not in STRIKEABLE or not (entry.sent_text or "").strip():
            # no_answer не прячем под зачёркиванием; без сохранённого текста
            # править нечем. Решение окончательное — отмечаем.
            entry.struck_at = now
            continue

        tail = await _closing_tail(session, entry, interaction, outcome, now)
        text = build_struck_text(entry.sent_text or "", entry.kind, outcome, tail)

        # Кнопка «Показать переписку» — только в личке с правом на выписку
        # (не в группе и не у «своего» алерта сотрудника).
        personal = await _self_target(session, interaction)
        plain_ids = {personal.tg_user_id} if personal is not None else set()
        markup = _context_kb(entry.chat_id, entry.opened_by_message_id)

        edited = 0
        last_error: str | None = None
        for raw_id, message_id in (entry.message_ids or {}).items():
            try:
                target = int(raw_id)
            except (TypeError, ValueError):
                continue
            if target < 0 and target != effective_notify_group_id():
                # Правим только действующую группу уведомлений: старый номер
                # после переезда в супергруппу заслон отвергнет (ERROR «outbound.blocked»).
                continue
            try:
                await bot.edit_message_text(
                    chat_id=target,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=(
                        markup if target > 0 and target not in plain_ids else None
                    ),
                )
                edited += 1
            except Exception as exc:  # noqa: BLE001 — отказ штатный, тик не роняем
                last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.info(
                    "alerts.strike_failed",
                    chat_id=entry.chat_id,
                    target=target,
                    error=last_error,
                )

        entry.struck_at = now
        if edited:
            struck += 1
            log.info(
                "alerts.struck", kind=entry.kind, chat_id=entry.chat_id, outcome=outcome
            )
        elif last_error:
            entry.last_error = last_error

    return struck


async def _covered_still_waiting(session: AsyncSession, alert_id: int) -> bool:
    """По алерту есть покрытая просрочка, которая всё ещё ждёт ответа.

    Покрытые оповещены тем же сообщением, поэтому алерт закрывается только
    когда закрыты все они. Исход считается `alert_outcome`; ждущая — только `waiting`.
    """
    from app.services.alert_digest import alert_outcome
    from app.services.dismissals import is_dismissed

    rows = (
        await session.execute(
            select(AlertLog, Interaction)
            .outerjoin(
                Interaction,
                (Interaction.chat_id == AlertLog.chat_id)
                & (Interaction.opened_by_message_id == AlertLog.opened_by_message_id),
            )
            .where(AlertLog.covered_by_id == alert_id)
        )
    ).all()
    for covered, interaction in rows:
        dismissed = await is_dismissed(
            session, covered.chat_id, covered.opened_by_message_id
        )
        if alert_outcome(covered.kind, interaction, dismissed) == "waiting":
            return True
    return False


async def _closing_tail(
    session: AsyncSession, entry, interaction, outcome: str, now: datetime
) -> str:
    """Приписка под зачёркнутым алертом — кто и за сколько закрыл кейс."""
    if outcome == "dismissed":
        row = (
            await session.execute(
                select(BotUser.display_name, BotUser.username)
                .select_from(BreachDismissal)
                .outerjoin(BotUser, BotUser.id == BreachDismissal.dismissed_by)
                .where(BreachDismissal.chat_id == entry.chat_id)
                .where(
                    BreachDismissal.opened_by_message_id == entry.opened_by_message_id
                )
                .limit(1)
            )
        ).first()
        who = (row[0] or row[1]) if row is not None else None
        return f"Закрыто вручную{f': {escape(who)}' if who else ''}."
    if outcome == "no_need":
        return "Ответа не требовалось: вопроса клиент не задавал."

    if entry.kind == KIND_NO_REACTION:
        staff_id = interaction.first_reaction_staff_id
        work, replied_at = (
            interaction.ttfr_business_seconds,
            interaction.first_reaction_at,
        )
        role = "менеджер отработал"
    else:
        staff_id = interaction.substantive_staff_id
        work, replied_at = (
            interaction.ttfa_business_seconds,
            interaction.substantive_at,
        )
        role = "специалист отработал"

    who: str | None = None
    if staff_id is not None:
        from app.db.models import Staff

        person = await session.get(Staff, staff_id)
        if person is not None:
            who = person.full_name
    if work is None:
        timing = "время не посчитано"
    else:
        timing = f"за {fmt_duration(work)} рабочего времени"
        if replied_at is not None and entry.sent_at is not None:
            after_alert = int((replied_at - entry.sent_at).total_seconds())
            if after_alert > 0:
                timing += f", или спустя {fmt_duration(after_alert)} после алерта"
            else:
                # Ответ раньше отправки алерта (алерт ждал рабочего окна или доставки).
                timing += ", ещё до отправки алерта"
    # Сотрудник не определён — ответ засчитан компании.
    answered = "компания ответила" if who is None else f"ответил(а) {escape(who)}"
    return f"{role.capitalize()}: {answered} {timing}."
