"""Обходчик экранов: нажать ВСЕ читающие кнопки и дожить до ответа.

Ловит NameError в теле обработчика и кнопки, которые не матчатся ни одним
обработчиком и молча крутят спиннер: тесты зовут сервисы напрямую,
а verify_bot проверяет упаковку кнопок, но не то, что обработчик ДОЖИВАЕТ
до ответа.

Каждый callback скармливается настоящему диспетчеру от имени
синтетического владельца (роль с максимумом прав — открывает все экраны).
Успех: обработчик сделал хотя бы один исходящий вызов и не бросил ничего,
кроме сетевой ошибки регистратора. Провал: любое другое исключение
(NameError, KeyError, AttributeError...) или полное молчание — кнопка-сирота.

⚠️ Только читающие кнопки. Мутирующие (track/pause/approve/bind/ignore...)
сюда не входят сознательно: скрипт гоняется и на боевой базе.
Несуществующие id (999999) — это тоже проверка: обработчик обязан ответить
«не найдено», а не упасть.
"""

import asyncio

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import Update, User
from sqlalchemy import delete, select

from app.bot.callbacks import (
    AlertAction,
    ChatAction,
    DismissedAction,
    DrillAction,
    HelpNav,
    LabAction,
    MarkupAction,
    Nav,
    ReportAction,
    SettingAction,
    StaffAction,
    UserAction,
)
from app.bot.guard import create_bot
from app.bot.main import build_dispatcher, setup_logging
from app.config import get_settings
from app.db.base import session_scope
from app.db.models import BotRole, BotUser, BotUserState, ChatState, TelegramUpdate

setup_logging("CRITICAL")

BOT_ID = 7000000001
BOT_USERNAME = "example_sla_bot"
AUDIT_TG_ID = 991000003  # синтетический, отличен от других verify-скриптов
UPDATE_BASE = 9300001

MISSING = 999999  # заведомо несуществующий id: ждём «не найдено», не падение


def screens() -> list[tuple[str, str]]:
    """(подпись, callback data) всех читающих экранов."""
    probes: list[tuple[str, str]] = []

    for target in ("main", "reports", "chats", "staff", "users", "settings", "health", "audit", "help"):
        probes.append((f"раздел {target}", Nav(to=target).pack()))

    from app.services.help_text import PAGES

    for page, _title in (("hub", ""), *PAGES, ("nosuch", "")):
        probes.append((f"справка: {page}", HelpNav(page=page).pack()))

    probes += [
        ("пользователи: список", UserAction(action="list").pack()),
        ("пользователи: заявки", UserAction(action="pending").pack()),
        ("пользователи: меню приглашений", UserAction(action="invite_menu").pack()),
        ("пользователи: карточка (нет такого)", UserAction(action="view", user_id=MISSING).pack()),
        ("пользователи: привязка (нет такого)", UserAction(action="link", user_id=MISSING).pack()),
        ("пользователи: пропуск привязки (нет)", UserAction(action="link_skip", user_id=MISSING).pack()),
    ]

    probes += [
        ("сотрудники: специалисты", StaffAction(action="list_s").pack()),
        ("сотрудники: менеджеры", StaffAction(action="list_m").pack()),
        ("сотрудники: без роли", StaffAction(action="list_u").pack()),
        ("сотрудники: очередь разметки", StaffAction(action="unresolved").pack()),
        ("сотрудники: карточка (нет такого)", StaffAction(action="view", staff_id=MISSING).pack()),
        ("сотрудники: экран роли (нет такого)", StaffAction(action="role", staff_id=MISSING).pack()),
        ("разметка: не сотрудники", MarkupAction(action="ignored").pack()),
        ("разметка: подпись (нет такой)", MarkupAction(action="pick", msg_id=MISSING).pack()),
        ("разметка: карточка не-сотрудника (нет)", MarkupAction(action="iview", msg_id=MISSING).pack()),
    ]

    for state in ChatState:
        probes.append(
            (f"чаты: список {state.value}", ChatAction(action="list", value=state.value).pack())
        )
    probes.append(("чаты: карточка (нет такого)", ChatAction(action="view", chat_id=MISSING).pack()))
    probes.append(("чаты: форма поиска", ChatAction(action="search").pack()))

    probes += [
        ("отчёты: выбор чата", ReportAction(action="scope", scope="chat").pack()),
        ("отчёты: выбор сотрудника", ReportAction(action="scope", scope="staff").pack()),
        ("отчёты: сводный периоды", ReportAction(action="scope", scope="all").pack()),
        ("отчёты: мои показатели", ReportAction(action="scope", scope="self").pack()),
        ("отчёты: сводный за сегодня", ReportAction(action="run", scope="all", period="today").pack()),
        (
            "отчёты: по сотруднику (нет такого)",
            ReportAction(action="run", scope="staff", period="today", target_id=MISSING).pack(),
        ),
        (
            "отчёты: по чату (нет такого)",
            ReportAction(action="run", scope="chat", period="today", target_id=MISSING).pack(),
        ),
    ]

    for kind in ("wait", "brre", "brsp", "breach", "handoff", "noans", "noneed"):
        probes.append((f"срез {kind}", DrillAction(kind=kind, period="today").pack()))

    for kind in ("attention", "speed", "load", "night"):
        probes.append((f"лаборатория: {kind}", LabAction(kind=kind, period="today").pack()))
    probes += [
        ("сводка по алертам (сейчас)", LabAction(kind="adig").pack()),
        ("снятые алерты", DismissedAction().pack()),
        ("снятые алерты: 30 дней", DismissedAction(period="d30").pack()),
        (
            "снятые алерты: произвольный период",
            DismissedAction(period="c20260801-20260815").pack(),
        ),
        ("лаборатория: закрытое меню (menu)", LabAction(kind="menu").pack()),
        ("лаборатория: убранный отчёт (chats)", LabAction(kind="chats", period="today").pack()),
        ("лаборатория: убранный отчёт (shadow)", LabAction(kind="shadow", period="today").pack()),
    ]

    probes += [
        ("настройки: раздел алертов", SettingAction(action="section", section="alerts").pack()),
        (
            "настройки: поле порога",
            SettingAction(action="edit", section="alerts", key="threshold_minutes").pack(),
        ),
        # После формы ввода FSM ждёт текст — возврат в раздел обязан снять
        # ожидание; сам обработчик секции это и проверяет.
        ("настройки: назад в раздел", SettingAction(action="section", section="alerts").pack()),
    ]

    probes += [
        ("переписка (нет такой)", AlertAction(action="ctx", chat_id=MISSING, msg_id=MISSING).pack()),
        ("переписка: листание (нет такой)", AlertAction(action="ctxb", chat_id=MISSING, msg_id=MISSING).pack()),
        ("нарушение из среза (нет такого)", AlertAction(action="ctxd", chat_id=MISSING, msg_id=MISSING).pack()),
        ("снятие нарушения (нет такого)", AlertAction(action="dis", chat_id=MISSING, msg_id=MISSING).pack()),
    ]

    return probes


class OutboundRecorder(BaseRequestMiddleware):
    """Записывает исходящие вызовы и рвёт сеть — до Telegram ничего не уходит."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, make_request, bot, method):
        name = type(method).__name__
        if name != "GetMe":
            self.calls.append((name, str(getattr(method, "text", ""))[:60]))
        raise TelegramNetworkError(method=method, message="no-network (тест)")


def callback_update(update_id: int, data: str) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "from": {"id": AUDIT_TG_ID, "is_bot": False, "first_name": "Аудит"},
                "chat_instance": "audit",
                "data": data,
                "message": {
                    "message_id": 700,
                    "date": 1756400000,
                    "chat": {"id": AUDIT_TG_ID, "type": "private", "first_name": "Аудит"},
                    "from": {
                        "id": BOT_ID,
                        "is_bot": True,
                        "first_name": "SLA Monitor",
                        "username": BOT_USERNAME,
                    },
                    "text": "экран",
                },
            },
        }
    )


async def _ensure_auditor() -> None:
    async with session_scope() as session:
        user = await session.scalar(
            select(BotUser).where(BotUser.tg_user_id == AUDIT_TG_ID)
        )
        if user is None:
            session.add(
                BotUser(
                    tg_user_id=AUDIT_TG_ID,
                    display_name="Обходчик экранов",
                    role=BotRole.OWNER,
                    state=BotUserState.ACTIVE,
                    permissions={},
                )
            )
        else:
            user.role = BotRole.OWNER
            user.state = BotUserState.ACTIVE


async def _cleanup(update_count: int) -> None:
    async with session_scope() as session:
        await session.execute(
            delete(TelegramUpdate).where(
                TelegramUpdate.update_id.between(UPDATE_BASE, UPDATE_BASE + update_count)
            )
        )
        await session.execute(
            delete(BotUser).where(BotUser.tg_user_id == AUDIT_TG_ID)
        )


async def main() -> None:
    settings = get_settings()
    recorder = OutboundRecorder()
    bot = create_bot(settings.require_bot_token())
    bot.session.middleware(recorder)
    bot._me = User(
        id=BOT_ID,
        is_bot=True,
        first_name="SLA Monitor",
        username=BOT_USERNAME,
        can_join_groups=True,
        can_read_all_group_messages=True,
        supports_inline_queries=False,
    )

    dispatcher = build_dispatcher()
    await _ensure_auditor()

    probes = screens()
    failures: list[str] = []
    update_id = UPDATE_BASE

    for label, data in probes:
        before = len(recorder.calls)
        try:
            await dispatcher.feed_update(bot, callback_update(update_id, data))
        except (TelegramNetworkError, TelegramBadRequest):
            pass  # регистратор рвёт сеть после записи вызова — это успех
        except Exception as exc:  # noqa: BLE001 — ради этого скрипт и существует
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
            update_id += 1
            continue
        update_id += 1

        if len(recorder.calls) == before:
            failures.append(f"{label}: ни одного исходящего вызова — кнопка-сирота")
        else:
            print(f"  ok    {label}")

    await _cleanup(len(probes))
    print("  ok    синтетический пользователь и журнал обхода удалены")
    await bot.session.close()

    if failures:
        print()
        for line in failures:
            print(f"  FAIL  {line}")
        raise SystemExit(f"Обход экранов: {len(failures)} провал(ов) из {len(probes)}")

    print()
    print(f"ВСЕ ЭКРАНЫ ОТВЕЧАЮТ: нажато кнопок — {len(probes)}")


asyncio.run(main())
