"""Проверка: бот не отвечает в группе, даже если его тегают.

Тег и реплай на бота Telegram доставляет ему даже при включённом privacy
mode — это самый вероятный путь случайного ответа. Тест скармливает
диспетчеру групповые апдейты всех подозрительных видов и записывает каждый
исходящий вызов Bot API. Ожидание: ноль исходящих.

Контрольный положительный случай — /start в личке — обязан породить
исходящее сообщение: он доказывает, что регистратор вообще ловит отправки.
"""

import asyncio

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Update

from app.bot.guard import GroupWriteAttempt, create_bot
from app.bot.main import build_dispatcher
from app.config import get_settings

BOT_ID = 7000000001
BOT_USERNAME = "example_sla_bot"
GROUP_ID = -100777000111
# Синтетическая группа уведомлений: заслон ей разрешает ОТПРАВКУ, но бот
# всё равно обязан молчать в ответ на теги и команды — исключение касается
# только алертов и отчётов, которые шлёт воркер, а не диалога.
NOTIFY_GROUP_ID = -100777000222
STRANGER_ID = 999888777


class OutboundRecorder(BaseRequestMiddleware):
    """Записывает каждый исходящий вызов и рвёт сеть — до Telegram не доходит."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def __call__(self, make_request, bot, method):
        name = type(method).__name__
        if name != "GetMe":
            self.calls.append((name, getattr(method, "chat_id", None)))
        raise TelegramNetworkError(method=method, message="no-network (тест)")


def group_update(
    update_id: int, text: str, reply_to_bot: bool = False, group_id: int = GROUP_ID
) -> Update:
    message = {
        "message_id": update_id,
        "date": 1755680000,
        "chat": {"id": group_id, "type": "supergroup", "title": "Тестовая группа"},
        "from": {"id": 555000111, "is_bot": False, "first_name": "Клиент"},
        "text": text,
    }
    if reply_to_bot:
        message["reply_to_message"] = {
            "message_id": 1,
            "date": 1755670000,
            "chat": {"id": group_id, "type": "supergroup", "title": "Тестовая группа"},
            "from": {
                "id": BOT_ID,
                "is_bot": True,
                "first_name": "SLA Monitor",
                "username": BOT_USERNAME,
            },
            "text": "старое сообщение",
        }
    return Update.model_validate({"update_id": update_id, "message": message})


def private_start(update_id: int) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 1755680000,
                "chat": {"id": STRANGER_ID, "type": "private", "first_name": "Незнакомец"},
                "from": {"id": STRANGER_ID, "is_bot": False, "first_name": "Незнакомец"},
                "text": "/start",
            },
        }
    )


async def main() -> None:
    settings = get_settings()
    # Свой синтетический id группы уведомлений, а не значение из .env:
    # смоук обязан проверять молчание в разрешённой группе на любой машине.
    settings.notify_group_chat_id = NOTIFY_GROUP_ID
    bot = create_bot(settings.require_bot_token())
    recorder = OutboundRecorder()
    bot.session.middleware(recorder)

    # bot.me() кэшируется заранее, чтобы фильтры команд не лезли в сеть.
    from aiogram.types import User

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

    print("Групповые апдейты, на которые бот мог бы ответить:")
    scenarios = [
        (9100001, group_update(9100001, f"привет @{BOT_USERNAME}, ты тут?"), "тег в тексте"),
        (9100002, group_update(9100002, f"/start@{BOT_USERNAME}"), "команда с упоминанием"),
        (9100003, group_update(9100003, "а это ответ боту", reply_to_bot=True), "реплай на бота"),
        (9100004, group_update(9100004, "/menu"), "голая команда"),
        # Группа уведомлений: заслон разрешает туда ОТПРАВКУ, но реагировать
        # на людей бот не должен и там — он в этой группе только публикует.
        (
            9100006,
            group_update(9100006, f"@{BOT_USERNAME} покажи отчёт", group_id=NOTIFY_GROUP_ID),
            "тег в группе уведомлений",
        ),
        (
            9100007,
            group_update(9100007, f"/start@{BOT_USERNAME}", group_id=NOTIFY_GROUP_ID),
            "команда в группе уведомлений",
        ),
        (
            9100008,
            group_update(9100008, "ответ боту", reply_to_bot=True, group_id=NOTIFY_GROUP_ID),
            "реплай в группе уведомлений",
        ),
    ]
    for _, update, label in scenarios:
        before = len(recorder.calls)
        try:
            await dispatcher.feed_update(bot, update)
        except (GroupWriteAttempt, TelegramNetworkError) as exc:
            raise AssertionError(f"«{label}»: бот попытался что-то отправить: {exc}")
        sent = recorder.calls[before:]
        assert not sent, f"«{label}»: исходящие вызовы: {sent}"
        print(f"  ok    {label}: ноль исходящих")

    print("Контроль: /start в личке ОБЯЗАН породить исходящее")
    try:
        await dispatcher.feed_update(bot, private_start(9100005))
    except TelegramNetworkError:
        pass  # регистратор рвёт сеть после записи — это ожидаемо
    private_sends = [c for c in recorder.calls if c[0] == "SendMessage"]
    assert private_sends and private_sends[0][1] == STRANGER_ID, (
        f"контроль не сработал: {recorder.calls}"
    )
    print(f"  ok    заглушка ушла в личку {STRANGER_ID} — регистратор ловит отправки")

    # Группа уведомлений исключена из аналитики: чат для неё не заводится
    # даже после сообщений в ней — инжест пропускает её целиком.
    from sqlalchemy import delete, select

    from app.db.base import session_scope
    from app.db.models import BotUser, Chat, TelegramUpdate

    async with session_scope() as session:
        notify_chat = await session.scalar(
            select(Chat).where(Chat.tg_chat_id == NOTIFY_GROUP_ID)
        )
        assert notify_chat is None, (
            "группа уведомлений попала в аналитику: для неё создан чат"
        )
    print("  ok    группа уведомлений не завела чат — аналитика её не видит")

    # Убираем следы синтетики из базы.
    async with session_scope() as session:
        await session.execute(
            delete(TelegramUpdate).where(TelegramUpdate.update_id.between(9100001, 9100008))
        )
        await session.execute(delete(BotUser).where(BotUser.tg_user_id == STRANGER_ID))
        await session.execute(delete(Chat).where(Chat.tg_chat_id == GROUP_ID))
    print("  ok    синтетические записи удалены из базы")

    await bot.session.close()
    print()
    print(
        "ПОДТВЕРЖДЕНО: на тег, команду и реплай в группе бот не отвечает — "
        "включая группу уведомлений"
    )


asyncio.run(main())
