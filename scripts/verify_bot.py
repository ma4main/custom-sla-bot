"""Проверка бота без Telegram: сборка, кнопки, права, заслон."""

import asyncio

failures: list[str] = []


def check(label: str, fn) -> None:
    try:
        fn()
        print(f"  ok    {label}")
    except Exception as exc:
        failures.append(label)
        print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")


def main() -> None:
    print("1. Диспетчер собирается, все роутеры и фильтры на месте")
    from app.bot.main import build_dispatcher

    check("build_dispatcher()", build_dispatcher)

    print("2. Все callback-кнопки упаковываются и распаковываются")
    from app.bot.callbacks import AlertAction, ChatAction, DrillAction, HelpNav, LabAction, MarkupAction, Nav, ReportAction, SenderAction, SettingAction, StaffAction, UserAction
    from app.db.models import ChatState

    samples = [
        *[Nav(to=t) for t in ("main", "reports", "chats", "staff", "users", "settings", "health", "help")],
        HelpNav(page="hub"),
        HelpNav(page="time"),
        *[ChatAction(action="list", value=s.value, page=3) for s in ChatState],
        ChatAction(action="view", chat_id=42, page=1),
        ChatAction(action="track", chat_id=42, page=0),
        ChatAction(action="track_all"),
        ChatAction(action="pause", chat_id=42, page=0),
        ChatAction(action="archive", chat_id=42, page=0),
        ChatAction(action="delete", chat_id=42, page=0),
        ChatAction(action="delete_do", chat_id=42, page=0),
        StaffAction(action="list", page=2),
        StaffAction(action="list_s", page=1),
        StaffAction(action="list_m", page=1),
        StaffAction(action="list_u", page=1),
        StaffAction(action="view", staff_id=7, page=0),
        StaffAction(action="role", staff_id=7, page=0),
        *[StaffAction(action=a, staff_id=7, page=0) for a in ("role_s", "role_m", "role_a")],
        StaffAction(action="activate", staff_id=7, page=0),
        StaffAction(action="deactivate", staff_id=7, page=0),
        StaffAction(action="add"),
        StaffAction(action="alias", staff_id=7, page=0),
        StaffAction(action="unresolved"),
        UserAction(action="list", page=1),
        UserAction(action="view", user_id=5, page=0),
        UserAction(action="set_role", user_id=5, role="admin"),
        UserAction(action="disable", user_id=5, page=0),
        UserAction(action="enable", user_id=5, page=0),
        UserAction(action="transfer", user_id=5),
        UserAction(action="co_owner", user_id=5),
        UserAction(action="co_owner_do", user_id=5),
        UserAction(action="transfer_do", user_id=5),
        UserAction(action="pending"),
        UserAction(action="approve", user_id=5, role="manager"),
        UserAction(action="invite_menu"),
        UserAction(action="invite_create", role="admin"),
        UserAction(action="link_skip", user_id=5, page=0),
        SettingAction(action="section", section="work_calendar"),
        SettingAction(action="edit", section="alerts", key="threshold_minutes"),
        *[ReportAction(action="scope", scope=s) for s in ("all", "chat", "staff", "self")],
        ReportAction(action="run", scope="all", period="prev_week"),
        ReportAction(action="run", scope="chat", period="today", target_id=15),
        ReportAction(action="pick", scope="staff", target_id=1),
        ReportAction(action="target", scope="chat", target_id=15),
        ReportAction(action="expx", scope="all", period="last7"),
        ReportAction(action="expc", scope="staff", period="this_month", target_id=2),
        ReportAction(action="custom", scope="chat", target_id=15),
        ReportAction(action="run", scope="all", period="c20260801-20260815"),
        ReportAction(action="expx", scope="staff", period="c20260801-20260815", target_id=2),
        MarkupAction(action="pick", msg_id=13),
        MarkupAction(action="bind", msg_id=13, staff_id=1),
        MarkupAction(action="ignore", msg_id=13),
        MarkupAction(action="ignored"),
        MarkupAction(action="iview", msg_id=13),
        MarkupAction(action="restore", msg_id=13),
        # Разметка отправителя: худший случай — 13-значный ID бота, чат
        # анонимного админа и обе границы окна выписки в одной кнопке.
        SenderAction(action="card", key=1087968824),
        SenderAction(action="roles", key=6543210987654, chat_id=999999, msg_id=123456789, to_id=123456999),
        SenderAction(action="rnd", key=6543210987654, chat_id=999999, msg_id=123456789, to_id=123456999),
        SenderAction(action="bind", key=6543210987654, chat_id=999999, staff_id=4242, msg_id=123456789, to_id=123456999),
        SenderAction(action="auth", key=1087968824, chat_id=999999, staff_id=4242, msg_id=123456789, to_id=123456999),
        SenderAction(action="cli", key=6543210987654, chat_id=999999, msg_id=123456789, to_id=123456999),
        SenderAction(action="sys", key=6543210987654, chat_id=999999, msg_id=123456789, to_id=123456999),
        SenderAction(action="del", key=6543210987654, chat_id=999999, msg_id=123456789, to_id=123456999),
        SenderAction(action="parts", msg_id=123456789, to_id=123456999),
        AlertAction(action="ctx", chat_id=999999, msg_id=123456789),
        AlertAction(action="ctxm", chat_id=999999, msg_id=123456789),
        AlertAction(action="ctxb", chat_id=999999, msg_id=123456789),
        AlertAction(action="ctxf", chat_id=999999, msg_id=123456789),
        AlertAction(action="ctxx", chat_id=999999),
        AlertAction(action="ctxd", chat_id=999999, msg_id=123456789),
        AlertAction(action="dis", chat_id=999999, msg_id=123456789),
        AlertAction(action="undis", chat_id=999999, msg_id=123456789),
        *[
            DrillAction(kind=k, period="prev_week")
            for k in ("brre", "brsp")
        ],
        SettingAction(action="cycle", section="alerts", key="substantive_mode"),
        SettingAction(action="setv", section="digest", key="evening_enabled", value="yes"),
        SettingAction(action="setv", section="alerts", key="substantive_mode", value="on"),
        LabAction(kind="menu"),
        *[LabAction(kind=k, period="this_month") for k in ("attention", "speed", "chats", "load", "night", "shadow", "html")],
        # Проваливание из отчёта: худший случай по длине — произвольный
        # период плюс большие id, обязан влезать в 64 байта.
        *[DrillAction(kind=k, period="prev_week") for k in ("breach", "handoff", "noans", "noneed")],
        DrillAction(kind="handoff", period="c20260801-20260815", chat_id=999999, page=42),
    ]
    for sample in samples:
        packed = sample.pack()
        assert len(packed.encode()) <= 64, f"больше 64 байт: {packed}"
        type(sample).unpack(packed)
    print(f"  ok    упаковано и распаковано: {len(samples)} кнопок, все в лимите 64 байта")

    # Русские подписи настроек: каждый ПОКАЗЫВАЕМЫЙ ключ должен иметь
    # подпись, иначе новая настройка молча покажется владельцу сырым ключом.
    # Скрытые с экрана (служебная память версий и т. п.) сюда
    # не входят: их не видно, а подпись обещала бы кнопку, которой нет.
    from app.services.settings_store import DEFAULTS, FIELD_LABELS, visible_fields

    shown = {
        section: visible_fields(section, fields) for section, fields in DEFAULTS.items()
    }
    unlabeled = [
        f"{section}.{key}"
        for section, keys in shown.items()
        for key in keys
        if key not in FIELD_LABELS.get(section, {})
    ]
    assert not unlabeled, f"параметры без русской подписи: {unlabeled}"
    print(f"  ok    русские подписи есть у всех {sum(len(k) for k in shown.values())} видимых параметров")

    # Блоки раздела ссылаются только на настоящие ключи: опечатка в группе
    # молча выкидывала бы поле из своего блока в хвост экрана.
    from app.services.settings_store import FIELD_GROUPS

    ghost = [
        f"{section}.{key}"
        for section, groups in FIELD_GROUPS.items()
        for _, keys in groups
        for key in keys
        if key not in DEFAULTS.get(section, {})
    ]
    assert not ghost, f"в блоках настроек несуществующие ключи: {ghost}"
    print("  ok    блоки настроек ссылаются на существующие поля")

    # Приглашение копируется кнопкой, а не нажимается: ссылка одноразовая,
    # и выпустивший, тапнув по ней, погасил бы её на себе.
    from app.bot.keyboards import invite_created, invite_roles
    from app.services.access import ASSIGNABLE_ORDER, ROLE_LABELS

    link = "https://t.me/example_sla_bot?start=TESTCODE"
    invite_buttons = [b for row in invite_created(link).inline_keyboard for b in row]
    copy_buttons = [b for b in invite_buttons if b.copy_text is not None]
    assert len(copy_buttons) == 1, "кнопки «скопировать ссылку» нет или их несколько"
    assert copy_buttons[0].copy_text.text == link, "кнопка копирует не ту строку"
    assert not any(b.url for b in invite_buttons), "приглашение снова кликабельно"

    # Роли предлагаются только разрешённые и подписаны по-русски.
    role_buttons = [
        b
        for row in invite_roles().inline_keyboard
        for b in row
        if b.callback_data and UserAction.unpack(b.callback_data).action == "invite_create"
    ]
    offered = {UserAction.unpack(b.callback_data).role for b in role_buttons}
    assert offered == {role.value for role in ASSIGNABLE_ORDER}, f"роли приглашений: {offered}"
    assert {b.text for b in role_buttons} == {
        f"🎟 {ROLE_LABELS[role]}" for role in ASSIGNABLE_ORDER
    }, "подписи ролей разошлись с ROLE_LABELS"
    print(f"  ok    приглашение: копируется кнопкой, роли по-русски ({len(offered)} шт.)")

    print("3. Меню по ролям: состав разделов")
    from app.bot.keyboards import main_menu
    from app.db.models import BotRole, BotUser, BotUserState

    # Админ = владелец минус владение: разделы у них одинаковые,
    # разница живёт внутри карточек пользователей (кнопок владения у админа нет).
    expected = {
        BotRole.OWNER: {"reports", "chats", "staff", "users", "settings", "health", "help"},
        BotRole.ADMIN: {"reports", "chats", "staff", "users", "settings", "health", "help"},
        BotRole.MANAGER: {"reports", "help"},
    }
    for role, sections in expected.items():
        user = BotUser(tg_user_id=1, role=role, permissions={}, state=BotUserState.ACTIVE)
        markup = main_menu(user)
        got = {
            btn.callback_data.split(":", 1)[1]
            for row in markup.inline_keyboard
            for btn in row
        }
        assert got == sections, f"{role.value}: ожидалось {sections}, получено {got}"
        print(f"  ok    {role.value}: {sorted(got)}")

    disabled = BotUser(tg_user_id=1, role=BotRole.OWNER, permissions={}, state=BotUserState.DISABLED)
    assert not main_menu(disabled).inline_keyboard, "отключённый владелец не должен видеть меню"
    print("  ok    отключённый пользователь: меню пустое")

    from app.bot.keyboards import reports_menu

    def _menu_actions(role) -> set[str]:
        user = BotUser(tg_user_id=1, role=role, permissions={}, state=BotUserState.ACTIVE)
        return {
            btn.callback_data
            for row in reports_menu(user).inline_keyboard
            for btn in row
            if btn.callback_data
        }

    # Раздела лаборатории нет: сводка по алертам — в обычном меню отчётов,
    # меню лаборатории не всплывает.
    owner_menu = _menu_actions(BotRole.OWNER)

    assert any("lab:adig" in data for data in owner_menu), "владелец не видит сводку по алертам"
    assert not any("lab:menu" in data for data in owner_menu), "владелец видит лабораторию"
    assert not any("lab:html" in data for data in owner_menu), "владелец видит HTML-дашборд"
    # В КОРНЕ меню — только то, что не про конкретных людей. «Скорость
    # по сотрудникам» и «Работа вне графика» живут внутри «По сотруднику»,
    # их доступность проверяет verify_reports_access.
    for kind in ("attention", "load"):
        assert any(
            f"lab:{kind}" in data for data in owner_menu
        ), f"владелец не видит отчёт {kind}, переехавший в основные"
    for kind in ("speed", "night"):
        assert not any(
            f"lab:{kind}" in data for data in owner_menu
        ), f"отчёт {kind} снова всплыл в корне меню"
    # У администратора те же отчёты, что у владельца; у сотрудника —
    # только свои показатели, никаких сводных.
    assert any("lab:attention" in data for data in _menu_actions(BotRole.ADMIN))
    assert not any("lab:" in data for data in _menu_actions(BotRole.MANAGER)), (
        "сотрудник видит сводные отчёты"
    )

    # Роль владельца не выдаётся ни одним путём выдачи, а не только change_role:
    # callback data — недоверенный ввод, и UI без кнопки ничего не гарантирует.
    from app.services.access import (
        ASSIGNABLE_ROLES, AccessError, approve_user, change_role, create_invite,
    )

    async def _all_paths_guard() -> None:
        owner = BotUser(id=1, tg_user_id=1, role=BotRole.OWNER, permissions={}, state=BotUserState.ACTIVE)
        victim = BotUser(id=2, tg_user_id=2, role=BotRole.MANAGER, permissions={}, state=BotUserState.PENDING)

        # Корутины создаются лениво: иначе первое же падение оставляет
        # остальные несозданными и тест шумит RuntimeWarning.
        attempts = (
            ("change_role", lambda: change_role(None, owner, victim, BotRole.OWNER)),
            ("approve_user", lambda: approve_user(None, owner, victim, BotRole.OWNER)),
            ("create_invite", lambda: create_invite(None, owner, BotRole.OWNER)),
        )
        for label, make in attempts:
            try:
                await make()
                raise AssertionError(f"{label}: роль владельца выдана напрямую")
            except AccessError:
                pass

    asyncio.run(_all_paths_guard())
    assert BotRole.OWNER not in ASSIGNABLE_ROLES
    print("  ok    роль владельца не выдаётся ни change_role, ни approve, ни приглашением")

    # Получатели уведомлений выбираются по праву, а не по названию роли:
    # иначе новая роль выше владельца выпадает из списка.
    import inspect as _inspect

    from app.services import alerts as _alerts
    from app.worker import main as _worker
    from app.bot.handlers import start as _start

    for module in (_alerts, _worker, _start):
        source = _inspect.getsource(module)
        assert "BotUser.role == BotRole.OWNER" not in source, (
            f"{module.__name__}: адресаты снова выбираются по роли"
        )
        assert "notification_recipients" in source or "personal_alert_recipients" in source, (
            f"{module.__name__}: нет выбора по праву"
        )
    print("  ok    алерты, сбой ИИ и заявки адресуются по праву, а не по роли")

    # Формулы в выгрузке: название чата задают участники группы.
    from app.services.export import _defuse

    for dangerous in ("=HYPERLINK(\"http://evil\")", "+CMD|calc", "-2+3", "@SUM(A1)"):
        assert _defuse(dangerous).startswith("'"), dangerous
    assert _defuse("ООО «Ромашка»") == "ООО «Ромашка»", "безопасное значение испорчено"
    assert _defuse(42) == 42, "число не должно превращаться в строку"
    print("  ok    формулы в XLSX/CSV обезврежены, обычные значения не тронуты")

    # HTML в динамике: имя сотрудника и название чата приходят из чужих рук.
    from app.text import esc

    assert esc('ООО "Ромашка" & <партнёры>') == "ООО &quot;Ромашка&quot; &amp; &lt;партнёры&gt;".replace("&quot;", '"')
    assert esc(None) == ""
    print("  ok    экранирование HTML: единый помощник app/text.py")

    print("4. Заслон fail-closed: перебор ВСЕХ методов Bot API")
    import inspect

    import aiogram.methods as tg_methods
    from aiogram.methods.base import TelegramMethod
    from app.bot.guard import _GROUP_READ_ALLOWLIST, GroupWriteAttempt, OutboundGroupGuard, blocked_reason

    all_methods = [
        name
        for name, cls in vars(tg_methods).items()
        if inspect.isclass(cls)
        and issubclass(cls, TelegramMethod)
        and cls is not TelegramMethod
        and "chat_id" in getattr(cls, "model_fields", {})
    ]
    assert len(all_methods) > 60, f"подозрительно мало методов: {len(all_methods)}"

    leaked = [
        name for name in all_methods
        if blocked_reason(name, -100123, None) is None and name not in _GROUP_READ_ALLOWLIST
    ]
    assert not leaked, f"методы прошли в группу: {leaked}"
    print(f"  ok    {len(all_methods)} методов с chat_id: заблокированы все, кроме {len(_GROUP_READ_ALLOWLIST)} читающих")

    assert blocked_reason("МетодКоторогоЕщёНет", -1, None) is not None, "неизвестный метод прошёл"
    assert blocked_reason("EditMessageText", None, "inline123") is not None, "inline-правка прошла"
    assert blocked_reason("SendMessage", 12345, None) is None, "личка заблокирована зря"
    assert blocked_reason("GetChat", -100123, None) is None, "чтение группы заблокировано зря"
    print("  ok    неизвестный метод блокируется, inline-правки блокируются, личка и чтение проходят")

    # Группа уведомлений: единственная группа, куда бот пишет —
    # и только SendMessage/SendDocument. Проверяется своим синтетическим id,
    # а не значением из .env: смоук не должен зависеть от окружения машины.
    from app.bot.guard import _NOTIFY_GROUP_SEND_ALLOWLIST

    NOTIFY = -5551112223
    notify_allowed = _GROUP_READ_ALLOWLIST | _NOTIFY_GROUP_SEND_ALLOWLIST
    leaked = [
        name for name in all_methods
        if blocked_reason(name, NOTIFY, None, NOTIFY) is None and name not in notify_allowed
    ]
    assert not leaked, f"в группу уведомлений прошло лишнее: {leaked}"
    assert blocked_reason("SendMessage", NOTIFY, None, NOTIFY) is None, "текст в группу уведомлений не прошёл"
    assert blocked_reason("SendDocument", NOTIFY, None, NOTIFY) is None, "файл в группу уведомлений не прошёл"
    assert blocked_reason("EditMessageText", NOTIFY, None, NOTIFY) is None, "правка своего сообщения в группе уведомлений не прошла"
    assert blocked_reason("EditMessageText", -100123, None, NOTIFY) is not None, "правка в ЧУЖОЙ группе прошла"
    assert blocked_reason("PinChatMessage", NOTIFY, None, NOTIFY) is not None, "пин в группе уведомлений прошёл"
    assert blocked_reason("SendMessage", -100123, None, NOTIFY) is not None, "чужая группа прошла при заданном исключении"
    assert blocked_reason("SendMessage", NOTIFY, None, None) is not None, "исключение сработало без настройки"
    assert blocked_reason("SendMessage", str(NOTIFY), None, NOTIFY) is not None, "строковый id сошёл за группу уведомлений"
    print(
        "  ok    группа уведомлений: только "
        + "/".join(sorted(_NOTIFY_GROUP_SEND_ALLOWLIST))
        + f", остальные {len(all_methods) - len(_NOTIFY_GROUP_SEND_ALLOWLIST)} методов — блок"
    )

    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties

    async def guard_test() -> None:
        bot = Bot(token="1:test", default=DefaultBotProperties())
        bot.session.middleware(OutboundGroupGuard(notify_group_id=NOTIFY))
        attempts = [
            bot.send_message(chat_id=-100123, text="x"),
            bot.send_video_note(chat_id=-100123, video_note="x"),
            bot.ban_chat_member(chat_id=-100123, user_id=1),
            bot.stop_poll(chat_id=-100123, message_id=1),
            bot.send_message(chat_id="@somegroup", text="x"),
            # Даже в группе уведомлений разрешён не «всё», а два метода.
            bot.pin_chat_message(chat_id=NOTIFY, message_id=1),
            bot.send_poll(chat_id=NOTIFY, question="?", options=["а", "б"]),
        ]
        for coro in attempts:
            try:
                await coro
                raise AssertionError("вызов в группу НЕ заблокирован")
            except GroupWriteAttempt:
                pass
        await bot.session.close()

    asyncio.run(guard_test())
    print("  ok    живые вызовы (включая пин и опрос в группе уведомлений) блокируются")

    print("5. Worker и сервисы этапа 7 импортируются")
    import app.worker.main  # noqa: F401
    from app.services.calendar import business_seconds, response_deadline
    from datetime import datetime, timezone as _tz
    # пятница 19:00 -> понедельник 09:05 при графике пн-пт 9-18 = 5 минут
    cfg = {"weekdays": [1,2,3,4,5], "start": "09:00", "end": "18:00", "timezone": "UTC", "holidays": []}
    got = business_seconds(datetime(2026,8,14,19,0,tzinfo=_tz.utc), datetime(2026,8,17,9,5,tzinfo=_tz.utc), cfg)
    assert got == 300, f"рабочие секунды: ожидалось 300, получено {got}"
    print("  ok    worker импортируется; календарь: пт 19:00 -> пн 9:05 = 5 минут")

    print("5.1 Срок ответа: новый рабочий день даёт полный порог заново")
    day = {"weekdays": [1,2,3,4,5], "start": "10:00", "end": "19:00", "timezone": "UTC", "holidays": []}
    HALF = 30 * 60

    def _dl(moment):
        return response_deadline(moment, HALF, day)

    # Середина дня: срок ровно через 30 минут.
    assert _dl(datetime(2026,8,24,14,0,tzinfo=_tz.utc)) == datetime(2026,8,24,14,30,tzinfo=_tz.utc)
    # Ровно впритык к закрытию — ещё помещается в тот же день.
    assert _dl(datetime(2026,8,24,18,30,tzinfo=_tz.utc)) == datetime(2026,8,24,19,0,tzinfo=_tz.utc)
    # 18:40 при дне до 19:00: не помещается -> завтра 10:00 + 30 мин, а НЕ 10:10.
    got = _dl(datetime(2026,8,24,18,40,tzinfo=_tz.utc))
    assert got == datetime(2026,8,25,10,30,tzinfo=_tz.utc), got
    # Ночью и в выходные отсчёт стартует с открытия ближайшего рабочего дня.
    assert _dl(datetime(2026,8,24,23,0,tzinfo=_tz.utc)) == datetime(2026,8,25,10,30,tzinfo=_tz.utc)
    assert _dl(datetime(2026,8,22,12,0,tzinfo=_tz.utc)) == datetime(2026,8,24,10,30,tzinfo=_tz.utc)
    # Пятница 18:50 -> понедельник 10:30 (выходные и вечер не дают форы).
    assert _dl(datetime(2026,8,21,18,50,tzinfo=_tz.utc)) == datetime(2026,8,24,10,30,tzinfo=_tz.utc)
    # Праздник пропускается.
    holiday = dict(day, holidays=["2026-08-25"])
    got = response_deadline(datetime(2026,8,24,18,40,tzinfo=_tz.utc), HALF, holiday)
    assert got == datetime(2026,8,26,10,30,tzinfo=_tz.utc), got
    # Порог длиннее рабочего дня не должен уезжать в бесконечность.
    got = response_deadline(datetime(2026,8,24,14,0,tzinfo=_tz.utc), 20 * 3600, day)
    assert got is not None and got.date() == datetime(2026,8,25,tzinfo=_tz.utc).date(), got
    # Пустой список рабочих дней — недосохранённая настройка, а не «работаем
    # никогда»: как и business_seconds, откатываемся к будням по умолчанию.
    got = response_deadline(datetime(2026,8,24,14,0,tzinfo=_tz.utc), HALF, dict(day, weekdays=[]))
    assert got == datetime(2026,8,24,14,30,tzinfo=_tz.utc), got
    # Рабочих дней не найдено вообще: возвращаем None и не виснем в цикле.
    from datetime import date as _date, timedelta as _td
    all_holidays = [(_date(2026,8,24) + _td(days=i)).isoformat() for i in range(400)]
    assert response_deadline(datetime(2026,8,24,14,0,tzinfo=_tz.utc), HALF, dict(day, holidays=all_holidays)) is None
    print("  ok    срок: середина дня, впритык, вечер, ночь, выходные, праздник, кривой график")

    print("5.2 Здоровье ИИ: сбой глушит алерты, единичная ошибка — нет")
    from app.services.ai_health import (
        FAILURE_THRESHOLD, INITIAL, OUTCOME_BUDGET, OUTCOME_FAILURE, OUTCOME_IDLE,
        OUTCOME_SUCCESS, STATUS_DOWN, STATUS_OK, next_state,
    )
    moment = datetime(2026, 8, 24, 12, 0, tzinfo=_tz.utc)

    # Пустая очередь ничего не говорит о провайдере — состояние не меняется.
    assert next_state(None, OUTCOME_IDLE, None, moment) == dict(INITIAL)

    # Провайдер моргнул: одна-две ошибки не должны глушить алерты.
    state = dict(INITIAL)
    for expected in range(1, FAILURE_THRESHOLD):
        state = next_state(state, OUTCOME_FAILURE, "timeout", moment)
        assert state["status"] == STATUS_OK, state
        assert state["consecutive_failures"] == expected, state

    # Порог достигнут — сбой.
    state = next_state(state, OUTCOME_FAILURE, "timeout", moment)
    assert state["status"] == STATUS_DOWN and state["since"] == moment.isoformat(), state

    # Успех возвращает в строй и сбрасывает счётчик.
    state = next_state(state, OUTCOME_SUCCESS, None, moment)
    assert state["status"] == STATUS_OK and state["consecutive_failures"] == 0, state

    # Потолок токенов — сразу сбой, без накопления попыток.
    state = next_state(dict(INITIAL), OUTCOME_BUDGET, "лимит", moment)
    assert state["status"] == STATUS_DOWN and "лимит" in state["reason"], state
    print(f"  ok    сбой после {FAILURE_THRESHOLD} неудач подряд, лимит токенов — сразу, успех снимает")

    print("6. Алерты: текст обоих видов собирается, стороны и время на месте")
    from datetime import timedelta
    from app.db.models import BusinessSide, Interaction, InteractionState, Message, TransportActorKind
    from app.services.alerts import KIND_NO_REACTION, KIND_NO_SUBSTANTIVE, build_alert_text
    from app.services.transcript import SUBSTANTIVE_MEDIA, render_transcript, calendar_tz

    cal = {
        "weekdays": [1, 2, 3, 4, 5], "start": "10:00", "end": "19:00",
        "timezone": "Europe/Moscow", "holidays": [],
    }
    now = datetime(2026, 8, 24, 11, 30, tzinfo=_tz.utc)  # понедельник 14:30 МСК

    opener = Message(
        id=1, chat_id=2, thread_id=None, tg_message_id=10,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT,
        text="Добрый день, нужен акт сверки за июль <срочно>",
        sent_at=now - timedelta(minutes=40), has_media=False, media_kind=None,
    )
    episode = Interaction(
        chat_id=2, thread_id=None, opened_at=opener.sent_at, opened_by_message_id=1,
        last_client_at=now - timedelta(minutes=20), client_messages=2,
        state=InteractionState.OPEN,
    )
    last_client = Message(
        id=3, chat_id=2, thread_id=None, tg_message_id=12,
        transport_actor_kind=TransportActorKind.HUMAN_USER,
        business_side=BusinessSide.CLIENT, text="И ещё справку о доходах, пожалуйста",
        sent_at=now - timedelta(minutes=20), has_media=False, media_kind=None,
    )
    first = build_alert_text(
        kind=KIND_NO_REACTION, chat_title="Бухгалтерия: ООО Вектор", opener=opener,
        interaction=episode, last_client=last_client, reaction=None, last_company=None,
        deadline=now - timedelta(minutes=10), calendar_age=40 * 60, limit_minutes=30,
        calendar_cfg=cal, now=now,
    )
    assert "1 · Нет реакции" in first, first
    assert "👤 <b>Клиент</b>" in first and "🏢 <b>Компания не отвечала</b>" in first, first
    assert "Просрочено на <b>10 мин</b>" in first and "порог 30 мин" in first, first
    assert "надо было до сегодня в 14:20" in first, first
    assert "сегодня в 13:50" in first, first  # 40 минут назад по Москве
    assert "&lt;срочно&gt;" in first, "HTML в тексте клиента не экранирован"
    assert "всего сообщений: 2" in first, first
    assert "справку о доходах" in first, "последнее сообщение клиента не показано"

    # Служебный префикс интегратора в цитате не нужен: автор подписан строкой выше.
    reaction_message = Message(
        id=2, chat_id=2, thread_id=None, tg_message_id=11,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY,
        text="Ирина Соколова [corp.example.com] пишет:\n\nПриняла, уточню у бухгалтера",
        sent_at=now - timedelta(minutes=55), has_media=False, media_kind=None,
    )
    episode.state = InteractionState.REACTED
    episode.first_reaction_at = reaction_message.sent_at
    second = build_alert_text(
        kind=KIND_NO_SUBSTANTIVE, chat_title="Бухгалтерия: ООО Вектор", opener=opener,
        interaction=episode, last_client=last_client,
        reaction=(reaction_message, "Ирина Соколова"),
        last_company=(reaction_message, "Ирина Соколова"),
        deadline=now - timedelta(minutes=5), calendar_age=65 * 60, limit_minutes=60,
        calendar_cfg=cal, now=now,
    )
    assert "2 · Нет ответа специалиста" in second, second
    assert "Ирина Соколова" in second and "Просрочено на <b>5 мин</b>" in second, second
    assert "ответа специалиста после передачи ещё не было" in second, second
    assert "corp.example.com" not in second, "префикс интегратора не вычищен из цитаты"
    assert "«Приняла, уточню у бухгалтера»" in second, second

    # Компания писала после первой реакции — последнюю реплику тоже показываем.
    latest_company = Message(
        id=4, chat_id=2, thread_id=None, tg_message_id=13,
        transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
        business_side=BusinessSide.COMPANY, text="Запросила выписку в банке",
        sent_at=now - timedelta(minutes=10), has_media=False, media_kind=None,
    )
    third = build_alert_text(
        kind=KIND_NO_SUBSTANTIVE, chat_title="ООО Вектор", opener=opener,
        interaction=episode, last_client=last_client,
        reaction=(reaction_message, "Ирина Соколова"),
        last_company=(latest_company, "Нина Козлова"),
        deadline=now - timedelta(minutes=5), calendar_age=65 * 60, limit_minutes=60,
        calendar_cfg=cal, now=now,
    )
    assert "Последнее от компании" in third and "Нина Козлова" in third, third
    assert "Запросила выписку в банке" in third, third
    print("  ok    оба вида алерта: номер, стороны, время, автор, экранирование")

    transcript = render_transcript(
        [(opener, None), (reaction_message, "Ирина Соколова")],
        calendar_tz(cal), now, anchor_message_id=1,
    )
    # Стороны различаются цветом кружка.
    assert "▶️" in transcript and "🔵 клиент" in transcript, transcript
    assert "🟢 Ирина Соколова" in transcript, transcript
    assert "document" in SUBSTANTIVE_MEDIA and "sticker" not in SUBSTANTIVE_MEDIA
    print("  ok    выписка переписки: стороны, авторы, отметка обращения")

    # Второй вид алертов выключен по умолчанию, первый — нет.
    from app.services.settings_store import (
        MODE_OFF, MODE_ON, next_in_cycle, normalize_mode,
    )
    assert DEFAULTS["alerts"]["substantive_mode"] == MODE_OFF, "второй алерт не выключен"
    assert DEFAULTS["alerts"]["threshold_minutes"] == 30, "порог первой реакции сменился"

    # Кнопка ходит выключен↔включён; неизвестное значение, в том числе
    # прежнее «shadow», читается как «выключен», мусор не ломает.
    assert next_in_cycle("alerts", "substantive_mode", MODE_OFF) == MODE_ON
    assert next_in_cycle("alerts", "substantive_mode", MODE_ON) == MODE_OFF
    assert next_in_cycle("alerts", "substantive_mode", "чепуха") == MODE_ON
    assert next_in_cycle("alerts", "threshold_minutes", 30) is None, "не перечисление"
    assert normalize_mode("ВКЛЮЧИТЬ") == MODE_OFF and normalize_mode(None) == MODE_OFF
    assert normalize_mode(" Shadow ") == MODE_OFF
    print("  ok    режим второго алерта: off по умолчанию, кнопка off↔on, прежний shadow = off")

    print("7. Настройки, атрибуция, тихий режим, доставка алертов")
    from app.services.settings_store import visible_fields

    # Ключ, которого нет в DEFAULTS (остаток прежних версий в базе), на экран не попадает.
    stale = {**DEFAULTS["alerts"], "routing_strategy": "supervisors_only"}
    assert "routing_strategy" not in visible_fields("alerts", stale), "показан неизвестный ключ"
    visible_total = sum(len(visible_fields(s, DEFAULTS[s])) for s in DEFAULTS)
    assert visible_total > 0, "скрыли вообще всё"
    print(f"  ok    неизвестные ключи скрыты, рабочих настроек на экране: {visible_total}")

    # Массовая атрибуция идёт по id, а не по смещению.
    from app.services import attribution as _attribution

    attribution_source = _inspect.getsource(_attribution.attribute_all)
    assert ".offset(" not in attribution_source, "attribute_all снова использует offset"
    assert "Message.id > after_id" in attribution_source, "нет keyset-обхода"
    assert "MANUAL" in attribution_source, "ручная разметка не исключена из выборки"
    print("  ok    атрибуция обходит сообщения по id — партии не пропускаются")

    # Тихий режим ИИ реально исполняется.
    from app.services import episodes as _episodes
    from app.services.verdicts import SOURCE_MODEL, SOURCE_RULE

    episodes_source = _inspect.getsource(_episodes.rebuild_interactions)
    assert "ai_shadow_mode" in episodes_source, "тихий режим снова не исполняется"
    assert "Classification.source == SOURCE_RULE" in episodes_source, "нет фильтра по источнику"
    assert SOURCE_RULE != SOURCE_MODEL
    print("  ok    в тихом режиме вердикты модели на эпизоды не влияют")

    # Доставка алертов отделена от факта события.
    from app.services.alerts import MAX_DELIVERY_ATTEMPTS, retry_undelivered

    # Правило «дошло всем, а не хоть кому-то» проверяется поведением
    # в tests/test_alert_delivery.py. Здесь — только то, что уместно
    # проверять по тексту: недоставленное не закрывается и повторов
    # конечное число.
    process_source = _inspect.getsource(_alerts.process_alerts)
    assert "if not delivered_to:" in process_source, "недоставленный алерт закрывается"
    # Подмножество ID, а не сравнение длин: при смене состава получателей
    # длины совпадают, а множества нет.
    assert ".issubset(delivered_to)" in process_source, (
        "частичная доставка снова считается полной — второй получатель "
        "не узнает о просрочке никогда"
    )
    assert MAX_DELIVERY_ATTEMPTS >= 2 and callable(retry_undelivered)
    print(f"  ok    недоставленный алерт повторяется, попыток не больше {MAX_DELIVERY_ATTEMPTS}")

    # Валидация настроек: кривое значение не должно попасть в базу.
    from app.services.settings_rules import RULES, SettingError, check_section, validate

    for section in DEFAULTS:
        for key in visible_fields(section, DEFAULTS[section]):
            assert key in RULES.get(section, {}), f"{section}.{key}: нет правила проверки"

    bad = [
        ("work_calendar", "timezone", "Europe/Атлантида"),
        ("work_calendar", "start", "25:00"),
        ("work_calendar", "weekdays", "8"),
        ("work_calendar", "weekdays", ""),
        ("work_calendar", "holidays", "31.02.2026"),
        ("alerts", "threshold_minutes", "-5"),
        ("alerts", "threshold_minutes", "не знаю"),
        ("alerts", "enabled", "может быть"),
        ("alerts", "substantive_mode", "включить"),
        ("episodes", "max_messages", "0"),
    ]
    for section, key, value in bad:
        try:
            validate(section, key, value)
            raise AssertionError(f"{section}.{key}: принято мусорное {value!r}")
        except SettingError:
            pass

    assert validate("alerts", "threshold_minutes", " 45 ") == 45
    assert validate("alerts", "enabled", "да") is True
    assert validate("work_calendar", "weekdays", "1,2,3") == [1, 2, 3]
    assert validate("work_calendar", "timezone", "Europe/Moscow") == "Europe/Moscow"

    # Перевёрнутый рабочий день заглушил бы алерты навсегда.
    try:
        check_section("work_calendar", {"start": "19:00", "end": "10:00"})
        raise AssertionError("принят пустой рабочий день")
    except SettingError:
        pass
    check_section("work_calendar", {"start": "10:00", "end": "19:00"})
    print(f"  ok    настройки валидируются: {len(bad)} мусорных значений отклонены")

    # Правки сообщений возвращаются в обработку.
    from app.worker.main import reprocess_edited

    worker_source = _inspect.getsource(_worker)
    assert "needs_reclassification" in worker_source, "флаг правки снова никем не читается"
    assert "message.needs_reclassification = False" in worker_source, "флаг не гасится"
    assert callable(reprocess_edited)
    print("  ok    отредактированные сообщения переклассифицируются и переатрибутируются")

    # Активные дни считаются в рабочем поясе, а не в UTC.
    from app.services import report_data as _report_data

    report_source = _inspect.getsource(_report_data)
    assert "func.date(Message.sent_at)" not in report_source, "активные дни снова в UTC"
    assert "_local_day()" in report_source
    print("  ok    активные дни считаются по рабочему часовому поясу")

    print()
    if failures:
        raise SystemExit(f"ПРОВАЛЕНО: {failures}")
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")


main()
