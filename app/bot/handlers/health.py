"""Раздел «Состояние системы»: система сама объясняет своё состояние.

Технических кнопок нет — всё работает само, а неполадки (чужой бот в чатах,
сбои ИИ, очередь разметки) объявляются на этом экране.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app.bot.callbacks import Nav
from app.config import build_info, get_settings
from app.db.base import session_scope
from app.db.models import (
    BotUser,
    BotUserState,
    Chat,
    ChatState,
    Message,
    Staff,
    TelegramUpdate,
)
from app.services.access import Perm, notification_recipients
from app.services.ai_health import (
    STATUS_DOWN,
    failure_hint,
    failure_share,
    last_contact_at,
    latency_text,
    load_state,
    slow_since_at,
)
from app.services.ai_stats import (
    MAX_CLASSIFY_ATTEMPTS,
    cost_rub,
    last_verdict_at,
    month_tokens,
    pending_backlog,
    technical_verdicts,
    usage_by_day,
)
from app.services.attribution import count_unresolved

# Анонимный админ группы пишет от имени служебного бота Telegram: это не чужой
# бот, в предупреждение он не попадает.
from app.services.ingestion import ANONYMOUS_ADMIN_BOT_ID
from app.services.reprocess import REPLAYABLE_TYPES, unknown_bot_senders
from app.services.settings_store import get_section
from app.services.transcript import calendar_tz
from app.text import esc

log = structlog.get_logger(__name__)

router = Router(name="health")
# Только приватные диалоги: в группы бот не пишет и меню там не показывает.
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)


def _queue_line(pending: int, chats: int, oldest_at: datetime | None) -> str:
    """Очередь разметки: сколько, в скольких чатах и сколько ждёт старейшее."""
    if not pending:
        return "пусто"
    line = f"{pending} сообщ. в {chats} чат."
    if oldest_at is not None:
        minutes = max(0, int((datetime.now(timezone.utc) - oldest_at).total_seconds() // 60))
        waiting = f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч {minutes % 60} мин"
        line += f", самое старое ждёт {waiting}"
    return line


def _recipients_line(alert_cfg: dict, recipients: list[BotUser]) -> str:
    if not alert_cfg.get("enabled"):
        return "алерты выключены"
    if not recipients:
        return "🔴 никто"
    names = ", ".join(
        esc(user.display_name or user.username or str(user.tg_user_id))
        for user in recipients
    )
    return f"{len(recipients)} — {names}"


@router.callback_query(Nav.filter(F.to == "health"))
async def on_health(query: CallbackQuery, bot_user: BotUser) -> None:
    settings = get_settings()

    async with session_scope() as session:
        # Только переигрываемые типы: упавшие нажатия кнопок не повторяются.
        failed = await session.scalar(
            select(func.count(TelegramUpdate.update_id))
            .where(TelegramUpdate.processing_error.isnot(None))
            .where(TelegramUpdate.update_type.in_(REPLAYABLE_TYPES))
        ) or 0
        last_message_at = await session.scalar(select(func.max(Message.sent_at)))

        chats_tracked = await session.scalar(
            select(func.count(Chat.id)).where(Chat.state == ChatState.TRACKED)
        ) or 0
        chats_total = await session.scalar(select(func.count(Chat.id))) or 0
        staff_total = await session.scalar(select(func.count(Staff.id))) or 0
        # Тем же условием, что очередь разметки.
        unresolved = await count_unresolved(session)
        users_active = await session.scalar(
            select(func.count(BotUser.id)).where(BotUser.state == BotUserState.ACTIVE)
        ) or 0
        ai_health = await load_state(session)
        # Цифры про ИИ — теми же условиями, что у воркера.
        tokens_used = await month_tokens(session)
        daily = await usage_by_day(session, 7)
        queue, queue_chats, queue_oldest_at = await pending_backlog(
            session, settings.ai_accepted_models
        )
        last_verdict = await last_verdict_at(session)
        # Сообщения без разметки за сутки: попытки исчерпаны, движок их не читает.
        skipped_day, skipped_chats = await technical_verdicts(
            session, datetime.now(timezone.utc) - timedelta(days=1)
        )
        # Пульс воркера: ловит половинчатый отказ — бот отвечает, а воркер мёртв.
        from app.services.uptime import load_last_beat

        last_beat = await load_last_beat(session)
        # «Мёртвые письма»: алерты, исчерпавшие попытки, и апдейты
        # «записан, но не обработан».
        from app.db.models import AlertLog
        from app.services.alerts import MAX_DELIVERY_ATTEMPTS

        dead_alerts = await session.scalar(
            select(func.count(AlertLog.id))
            .where(AlertLog.delivered.is_(False))
            .where(AlertLog.shadow.is_(False))
            .where(AlertLog.attempts >= MAX_DELIVERY_ATTEMPTS)
        ) or 0
        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        oldest_unprocessed = await session.scalar(
            select(func.min(TelegramUpdate.received_at))
            .where(TelegramUpdate.processed_at.is_(None))
            .where(TelegramUpdate.processing_error.is_(None))
            .where(TelegramUpdate.received_at < stale_cutoff)
        )
        alert_cfg = await get_section(session, "alerts")
        calendar_cfg = await get_section(session, "work_calendar")
        alert_targets = await notification_recipients(session, Perm.ALERT_RECEIVE_ALL)
        for person in alert_targets:
            session.expunge(person)
        strangers = await unknown_bot_senders(session)

    warnings = []
    if settings.integrator_bot_id is None:
        warnings.append(
            "• <b>INTEGRATOR_BOT_ID не задан.</b> Сообщения из Битрикса не относятся "
            "к компании — разделение сторон не работает."
        )
    if chats_tracked == 0:
        warnings.append("• Ни один чат не включён в анализ.")
    if staff_total == 0:
        warnings.append(
            "• Справочник сотрудников пуст — авторов сообщений определить нельзя."
        )
    if failed:
        warnings.append(
            f"• Обновлений от Telegram с ошибкой обработки: {failed} — "
            "фоновый обработчик сам попробует переиграть их в течение суток."
        )

    # Чужой бот в чатах: его сообщения не относятся ни к одной стороне,
    # и метрики по чату искажаются.
    recent_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for stranger in strangers:
        if stranger["tg_user_id"] == ANONYMOUS_ADMIN_BOT_ID:
            continue  # анонимный админ группы — служебный бот Telegram
        if stranger["last_seen"] and stranger["last_seen"] < recent_cutoff:
            continue
        warnings.append(
            f"• ⚠️ <b>В чатах пишет неизвестный бот</b> "
            f"<code>{stranger['tg_user_id']}</code> "
            f"({stranger['messages']} сообщ.). Его сообщения не относятся "
            "ни к клиенту, ни к компании. Если это интегратор — его ID "
            "нужно добавить в конфигурацию сервера (INTEGRATOR_BOT_ID); "
            "после перезапуска история пересчитается сама."
        )

    # Кто получает алерты — на виду: ноль получателей должен быть заметен.
    if alert_cfg.get("enabled") and not alert_targets:
        warnings.append(
            "• 🔴 <b>Алерты включены, но получателей нет.</b> Ни один активный "
            "пользователь не имеет права получать алерты — просрочки никому "
            "не придут."
        )

    ai_state = "включён" if settings.ai_enabled else "выключен"
    fallback_model = ai_health.get("fallback")
    if settings.ai_enabled and fallback_model and ai_health.get("status") != STATUS_DOWN:
        # Резерв: алерты работают, но основная модель лежит — знать об этом
        # надо до того, как кончится и резерв.
        ai_state = f"работает на резерве ({esc(str(fallback_model).split('/')[-1])})"
        warnings.append(
            f"• ⚠️ <b>ИИ работает на резервной модели</b> "
            f"({esc(str(fallback_model))}). Основная не отвечает — "
            "классификация и алерты идут как обычно, но резерв размечает "
            "своим промптом. Система сама вернётся на основную, когда та "
            "поднимется; если держится дольше суток — проверьте провайдера."
        )
    if ai_health.get("status") == STATUS_DOWN:
        # Причина по-человечески — и в строке состояния, и в неполадках.
        hint = failure_hint(ai_health.get("reason"))
        ai_state = f"⚠️ {hint[0]}" if hint else "⚠️ сбой провайдера"
        since = ai_health.get("since")
        since_text = ""
        if since:
            try:
                moment = datetime.fromisoformat(since)
                local = moment.astimezone(calendar_tz(calendar_cfg))
                since_text = f" с {local.strftime('%d.%m %H:%M')}"
            except ValueError:
                since_text = ""
        # Обрезаем ДО экранирования: наоборот — «&amp;» рвётся посередине,
        # и битая сущность ломает разбор всего экрана.
        raw = esc(str(ai_health.get("reason") or "провайдер не отвечает")[:120])
        warnings.append(
            f"• <b>ИИ недоступен{since_text}.</b> "
            + (f"<b>{hint[0]}</b> ({raw})\n  {hint[1]}\n" if hint else f"{raw}\n")
            + "  <b>Алерты приостановлены</b>, чтобы не сыпались ложные срабатывания. "
            "Данные копятся, при восстановлении всё пересчитается."
        )
    elif not settings.ai_enabled:
        warnings.append(
            "• <b>ИИ выключен.</b> Смысловой фильтр не работает: обращения "
            "не отсеиваются по признаку «ответ не требуется»."
        )
    elif ai_health.get("degraded"):
        # Частичный сбой: провайдер роняет часть запросов; сообщения без вердикта
        # вернутся в очередь (`ai_stats.RETRY_BACKOFF_MINUTES`).
        from app.services.ai_stats import RETRY_BACKOFF_MINUTES

        warnings.append(
            f"• ⚠️ <b>ИИ сбоит частично:</b> за последние проходы упало "
            f"{failure_share(ai_health) or 0}% запросов к провайдеру. Алерты "
            "работают, но часть обращений пока без вердикта — они уйдут на "
            f"повторную классификацию через "
            f"{' и '.join(str(m) for m in RETRY_BACKOFF_MINUTES)} мин. "
            "Если держится дольше часа — проверьте провайдера и ключ."
        )

    # Медленный провайдер — не сбой: вердикты приходят, но разметка отстаёт.
    if (
        settings.ai_enabled
        and ai_health.get("slow")
        and ai_health.get("status") != STATUS_DOWN
    ):
        slow_since = slow_since_at(ai_health)
        warnings.append(
            "• 🐢 <b>Провайдер ИИ отвечает медленнее обычного</b>"
            + (f" (с {_ago(slow_since)})" if slow_since else "")
            + ". Разметка отстаёт, поэтому алерты о просрочке могут "
            "приходить позже. Бот повторяет разбор сам; если держится "
            "часами — проверьте провайдера."
        )
    if skipped_day:
        titles = ", ".join(f"«{esc(title)}»" for title in skipped_chats)
        warnings.append(
            f"• ⚠️ <b>Без разметки за сутки: {skipped_day}.</b> Бот исчерпал попытки "
            f"({MAX_CLASSIFY_ATTEMPTS}) и не дождался ответа модели — по этим "
            "сообщениям он ничего не подскажет"
            + (f". Чаты: {titles}" if titles else "")
            + ". Их стоит просмотреть глазами."
        )

    if settings.ai_monthly_token_limit:
        share = round(tokens_used * 100 / settings.ai_monthly_token_limit)
        usage_line = (
            f"{tokens_used:,} из {settings.ai_monthly_token_limit:,} токенов "
            f"({share}%)".replace(",", " ")
        )
        if share >= 80:
            warnings.append(
                f"• ⚠️ <b>Расход ИИ за месяц — {share}% потолка.</b> По исчерпании "
                "классификация остановится, алерты приостановятся до нового месяца."
            )
    else:
        usage_line = f"{tokens_used:,} токенов, потолок не задан".replace(",", " ")

    # Расход по дням за последнюю неделю. Рубли — по ценам из .env и только для
    # текущей модели; остатка счёта экран не показывает (у Cloud.ru нет API баланса).
    price_in, price_out = settings.ai_price_in_per_m, settings.ai_price_out_per_m
    day_parts = []
    for day, model, requests, prompt_tokens, completion_tokens in daily[:7]:
        cost = (
            cost_rub(int(prompt_tokens), int(completion_tokens), price_in, price_out)
            if model == settings.ai_model
            else None
        )
        tag = "" if model == settings.ai_model else f" ({esc(str(model).split('/')[-1])})"
        day_parts.append(
            f"{day:%d.%m} {int(requests)} запр., "
            f"{(int(prompt_tokens) + int(completion_tokens)) // 1000} тыс. ток."
            + (f", {cost:.0f} ₽" if cost is not None else "")
            + tag
        )
    usage_days_line = "; ".join(day_parts) or "нет данных"

    if dead_alerts:
        warnings.append(
            f"• 🔴 <b>Алертов, исчерпавших попытки доставки: {dead_alerts}.</b> "
            "Они больше не повторяются сами — проверьте адресатов "
            "(группа уведомлений, личные сообщения) и журнал алертов за последние "
            "дни."
        )
    if oldest_unprocessed is not None:
        warnings.append(
            f"• ⚠️ Есть обновления от Telegram, записанные, но не обработанные "
            f"(самое старое — {_ago(oldest_unprocessed)}). Фоновый обработчик "
            "переиграет их в течение суток; если строка не исчезает — смотреть "
            "логи бота."
        )

    beat_stale = last_beat is None or (
        datetime.now(timezone.utc) - last_beat > timedelta(minutes=5)
    )
    if beat_stale:
        warnings.append(
            "• 🔴 <b>Фоновый обработчик не подаёт признаков жизни</b> "
            f"(последний пульс: {_ago(last_beat)}). Алерты, рассылки и "
            "классификация стоят. Docker должен перезапустить его сам; если "
            "не проходит за несколько минут — смотреть сервер."
        )

    # Провенанс сборки: файл пишет scripts/deploy.sh.
    stamp = build_info()
    if stamp.get("sha"):
        built = stamp.get("built_at", "")[:16].replace("T", " ")
        version_line = f"<code>{esc(stamp['sha'])}</code> · собрано {esc(built)} UTC"
        if stamp.get("dirty") == "1":
            version_line += " · ⚠️ собрано из изменённой копии кода"
    else:
        version_line = "не помечена (сборка не через scripts/deploy.sh)"

    text = (
        "<b>Состояние системы</b>\n\n"
        f"Окружение: <code>{settings.environment}</code>\n"
        f"Версия: {version_line}\n"
        f"ИИ: {ai_state}\n"
        f"Расход ИИ за месяц: {usage_line}\n"
        f"Расход ИИ по дням: {usage_days_line}\n"
        f"Провайдер ИИ: последний живой контакт {_ago(last_contact_at(ai_health))}\n"
        f"Ответ провайдера: {latency_text(ai_health)}\n"
        f"Классификация: последний вердикт {_ago(last_verdict)}\n"
        f"Очередь разметки: {_queue_line(queue, queue_chats, queue_oldest_at)}\n"
        f"Без разметки за сутки: {skipped_day or 'нет'}\n"
        f"Фоновый обработчик: {'🔴 ' if beat_stale else ''}"
        f"последний пульс {_ago(last_beat)}\n"
        f"Последнее сообщение из чатов: {_ago(last_message_at)}\n\n"
        "<b>Справочники</b>\n"
        f"Чатов известно: {chats_total}, в анализе: {chats_tracked}\n"
        f"Сотрудников: {staff_total}\n"
        f"Нераспознанных авторов: {unresolved}\n"
        f"Алерты получают: {_recipients_line(alert_cfg, alert_targets)}\n"
        f"Активных пользователей бота: {users_active}\n"
    )
    if warnings:
        text += "\n<b>Неполадки</b>\n" + "\n".join(warnings)

    builder = InlineKeyboardBuilder()
    builder.button(text="📜 Журнал действий", callback_data=Nav(to="audit").pack())
    builder.button(text="‹ Назад", callback_data=Nav(to="main").pack())
    builder.adjust(1)

    await query.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await query.answer()


# Подписи действий журнала — по-русски. Неизвестное действие показывается
# как есть: лучше сырой ключ, чем пропавшая запись.
_AUDIT_LABELS = {
    "setting.changed": "изменил настройку",
    "chat.state_changed": "изменил состояние чата",
    "chat.deleted": "удалил чат",
    "chat.track_all": "включил все обнаруженные чаты",
    "staff.created": "добавил сотрудника",
    "staff.auto_created": "сотрудник добавлен автоматически из подписи Битрикса",
    "staff.role_changed": "изменил роль сотрудника",
    "staff.alias_added": "добавил написание имени",
    "staff.activated": "вернул сотрудника в работу",
    "staff.deactivated": "отметил: сотрудник больше не работает",
    "attribution.manual": "привязал подпись вручную",
    "attribution.not_staff": "пометил подпись «не сотрудник»",
    "attribution.restored": "вернул подпись в очередь разметки",
    "sender_rule.set": "решил, кто это (отправитель)",
    "sender_rule.cleared": "сбросил решение по отправителю",
    "user.approved": "одобрил заявку пользователя",
    "user.activated_by_invite": "вошёл по приглашению",
    "user.role_changed": "изменил роль пользователя",
    "user.disabled": "отключил пользователя",
    "user.enabled": "включил пользователя",
    "user.staff_linked": "связал учётную запись с сотрудником",
    "user.notify_personal": "переключил «уведомления в личные сообщения»",
    "ownership.co_owner_added": "сделал совладельцем",
    "ownership.transferred": "передал владение",
    "owner.bootstrap": "первый владелец назначен из конфигурации",
    "sysadmin.revoked": (
        "роль системного администратора снята, учётная запись стала администратором"
    ),
    "breach.dismissed": "снял нарушение («ответ не требуется»)",
    "breach.restored": "вернул нарушение",
    "notify_group.migrated": "группа уведомлений переехала",
}


# Подписи полей записи и типов объектов; неизвестные показываются как есть.
_PAYLOAD_LABELS = {
    "title": "название",
    "name": "имя",
    "from": "было",
    "to": "стало",
    "reason": "причина",
    "value": "значение",
    "role": "роль",
    "chat_id": "чат",
}
_OBJECT_LABELS = {
    "bot_user": "пользователь",
    "staff": "сотрудник",
    "attribution": "подпись",
    "chat": "чат",
    "interaction": "обращение",
    "setting": "настройка",
    "sender_rule": "решение по отправителю",
}


def _audit_details(row) -> str:
    from app.services.settings_store import field_label, section_label

    payload = row.payload or {}
    if row.action == "setting.changed":
        section = row.object_id or ""
        field = str(payload.get("field") or "")
        label = field_label(section, field) if field else ""
        return (
            f"{esc(section_label(section))} → {esc(label)}: "
            f"{esc(_render(payload.get('from')))} → <b>{esc(_render(payload.get('to')))}</b>"
        )
    parts = []
    for key in ("title", "name", "from", "to", "reason", "value", "role", "chat_id"):
        if key in payload and payload[key] not in (None, ""):
            label = _PAYLOAD_LABELS.get(key, key)
            parts.append(f"{label}: {esc(_render(payload[key]))}")
    if row.object_type and row.object_id:
        kind = _OBJECT_LABELS.get(row.object_type, row.object_type)
        ref = f"№{row.object_id}" if str(row.object_id).isdigit() else f"«{row.object_id}»"
        parts.append(f"объект: {esc(kind)} {esc(ref)}")
    return "; ".join(parts)


def _render(value) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "пусто"
    if value is None:
        return "—"
    return str(value)[:80]


AUDIT_LIMIT = 25


@router.callback_query(Nav.filter(F.to == "audit"))
async def on_audit(query: CallbackQuery, bot_user: BotUser) -> None:
    from app.db.models import AuditLog
    from app.services.access import has_perm

    if not (has_perm(bot_user, Perm.SYSTEM_HEALTH) or has_perm(bot_user, Perm.USER_MANAGE)):
        await query.answer("Недостаточно прав", show_alert=True)
        return

    async with session_scope() as session:
        calendar_cfg = await get_section(session, "work_calendar")
        rows = (
            await session.scalars(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(AUDIT_LIMIT)
            )
        ).all()
        actor_ids = {row.actor_user_id for row in rows if row.actor_user_id is not None}
        names: dict[int, str] = {}
        if actor_ids:
            for user in (
                await session.scalars(select(BotUser).where(BotUser.id.in_(actor_ids)))
            ).all():
                names[user.id] = user.display_name or user.username or str(user.tg_user_id)
        for row in rows:
            session.expunge(row)

    tz = calendar_tz(calendar_cfg)
    lines = [f"<b>📜 Журнал действий</b> — последние {AUDIT_LIMIT}", ""]
    if not rows:
        lines.append("Пока пусто.")
    for row in rows:
        who = names.get(row.actor_user_id, "система") if row.actor_user_id else "система"
        stamp = row.created_at.astimezone(tz).strftime("%d.%m %H:%M")
        label = _AUDIT_LABELS.get(row.action, row.action)
        details = _audit_details(row)
        lines.append(
            f"• {stamp} — <b>{esc(who)}</b> {esc(label)}"
            + (f"\n    {details}" if details else "")
        )

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data=Nav(to="audit").pack())
    builder.button(text="‹ К состоянию системы", callback_data=Nav(to="health").pack())
    builder.adjust(1)
    text = "\n".join(lines)
    if len(text) > 3900:
        # Обрезка — целыми записями: разрез посреди тега или «&amp;» Telegram не примет.
        kept: list[str] = []
        size = 0
        for line in lines:
            if size + len(line) + 1 > 3850:
                break
            kept.append(line)
            size += len(line) + 1
        text = "\n".join(kept) + "\n…"
    try:
        await query.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    except Exception as error:  # noqa: BLE001 — сбой показа не роняет обработчик
        # «Обновить» без изменений: пустая правка не ошибка.
        if "message is not modified" not in str(error):
            log.exception("audit.render_failed")
    await query.answer()


def _ago(moment: datetime | None) -> str:
    if moment is None:
        return "ещё не было"
    seconds = int((datetime.now(timezone.utc) - moment).total_seconds())
    if seconds < 90:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"

