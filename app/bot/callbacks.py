"""Callback data кнопок: всё управление ботом идёт кнопками (docs/SCREENS.md, раздел 0)."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="nav"):
    to: str


class ChatAction(CallbackData, prefix="chat"):
    """Действия над чатом.

    value — отдельное поле: двоеточие в action («list:tracked») ломает упаковку.
    Тип str | None: aiogram распаковывает пустую строку в None.
    """

    action: str
    value: str | None = None
    chat_id: int = 0
    page: int = 0


class StaffAction(CallbackData, prefix="staff"):
    action: str
    staff_id: int = 0
    page: int = 0


class UserAction(CallbackData, prefix="user"):
    action: str
    user_id: int = 0
    page: int = 0
    role: str | None = None


class SettingAction(CallbackData, prefix="set"):
    action: str
    section: str | None = None
    key: str | None = None
    value: str | None = None


class ReportAction(CallbackData, prefix="rep"):
    action: str
    scope: str | None = None
    period: str | None = None
    target_id: int = 0


class AlertAction(CallbackData, prefix="al"):
    """Кнопки под алертом. Всё нужное — в callback data: алерт шлёт воркер,
    кнопку обслуживает бот, общего состояния у них нет."""

    action: str
    chat_id: int = 0
    msg_id: int = 0


class DrillAction(CallbackData, prefix="dr"):
    """Проваливание из числа отчёта в список обращений.

    kind — срез (breach / handoff / noans), period — код периода из ReportAction,
    chat_id — 0 для сводного, id чата или DRILL_FROM_ATTENTION. Без FSM:
    список открывается и со старого сообщения.
    """

    kind: str
    period: str | None = None
    chat_id: int = 0
    page: int = 0


# Срез открыт из «Требует внимания»: возврат ведёт туда же. Сентинел в chat_id,
# а не новое поле — новое поле сломало бы кнопки в уже отправленных сообщениях.
DRILL_FROM_ATTENTION = -1


class DismissedAction(CallbackData, prefix="dsm"):
    """Экран «Снятые нарушения».

    period — свои коды: журнал считается по моменту решения и включает сегодня,
    а «последние 7 дней» в отчётах кончаются вчера. Произвольный период —
    строка «cГГГГММДД-ГГГГММДД», как в отчётах.
    """

    action: str = "open"
    period: str | None = None
    page: int = 0


class LabAction(CallbackData, prefix="lab"):
    kind: str
    period: str | None = None


class MarkupAction(CallbackData, prefix="mk"):
    """Ручная привязка нераспознанных авторов.

    raw_name в callback не влезает: кнопка несёт id примера (msg_id), а привязка
    применяется ко всем сообщениям с тем же raw_name.
    """

    action: str
    msg_id: int = 0
    staff_id: int = 0
    page: int = 0


class SenderAction(CallbackData, prefix="sr"):
    """Разметка «кто это» по отправителю.

    Отдельный класс, чтобы не менять формат callback-данных MarkupAction.
    key — Telegram ID отправителя; chat_id — внутренний id чата (0 — правило
    на все чаты). msg_id/to_id — границы окна выписки для «👥 Участники».
    """

    action: str
    key: int = 0
    chat_id: int = 0
    staff_id: int = 0
    msg_id: int = 0
    to_id: int = 0


class HelpNav(CallbackData, prefix="hlp"):
    """Экраны справки: «hub» — оглавление, остальное — ключи help_text.PAGES."""

    page: str = "hub"
