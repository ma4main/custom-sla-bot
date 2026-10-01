from __future__ import annotations

from aiogram.types import CallbackQuery, Message


class AnsweredAlready:
    """Callback, на который уже ответили: повторный answer — немой
    (Telegram принимает один answerCallbackQuery на нажатие).

    Нужен, когда обработчик показал своё всплывающее сообщение, а затем
    перерисовывает экран функцией, которая отвечает на нажатие сама. С
    `message=` оборачивает входящее сообщение: экран, рассчитанный на нажатие,
    рисуется ответом на текст (например, после поиска).
    """

    def __init__(
        self, query: CallbackQuery | None = None, *, message: Message | None = None
    ) -> None:
        self.message = message if message is not None else query.message

    async def answer(self, *args, **kwargs) -> None:
        return None
