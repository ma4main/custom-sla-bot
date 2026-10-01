"""Конфигурация из переменных окружения.

Здесь только то, без чего приложение не запустится: секреты, адреса, режимы.
Бизнес-настройки (календарь, пороги алертов, маршрутизация) живут в таблице setting
и меняются кнопками в боте — см. docs/SCREENS.md, раздел 8.
"""

from functools import lru_cache
from typing import Any

from pydantic import AwareDatetime, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Telegram ───────────────────────────────────────────────
    bot_token: str = Field(default="", alias="BOT_TOKEN")

    # Аккаунт, который станет владельцем при первом /start.
    bootstrap_owner_tg_id: int | None = Field(default=None, alias="BOOTSTRAP_OWNER_TG_ID")

    # ID бота-интегратора Bitrix24. Пока не задан, сообщения ботов — неизвестный
    # источник, и деление «клиент / компания» не работает.
    integrator_bot_id: int | None = Field(default=None, alias="INTEGRATOR_BOT_ID")

    # Единственная группа, куда бот пишет (алерты, отчёты). Только в .env, не кнопкой:
    # иначе алерты можно перенаправить в клиентский чат. Из аналитики исключена целиком.
    notify_group_chat_id: int | None = Field(default=None, alias="NOTIFY_GROUP_CHAT_ID")

    # callback_query обязателен: без него Telegram не доставляет нажатия кнопок.
    allowed_updates: str = Field(
        default="message,edited_message,callback_query,my_chat_member,chat_member",
        alias="ALLOWED_UPDATES",
    )

    # ── PostgreSQL ─────────────────────────────────────────────
    # Владелец схемы: миграции, бэкап.
    database_url: str = Field(default="", alias="DATABASE_URL")
    # Роль приложения только с DML; пусто — bot/worker ходят под DATABASE_URL.
    app_database_url: str = Field(default="", alias="APP_DATABASE_URL")

    # ── ИИ ─────────────────────────────────────────────────────
    ai_enabled: bool = Field(default=False, alias="AI_ENABLED")
    # Модель вызывается и вердикты пишутся, но движок учитывает только вердикты правил.
    ai_shadow_mode: bool = Field(default=True, alias="AI_SHADOW_MODE")
    ai_base_url: str = Field(default="", alias="AI_BASE_URL")
    ai_model: str = Field(default="", alias="AI_MODEL")
    ai_api_key: str = Field(default="", alias="AI_API_KEY")
    # Потолок токенов за месяц; при исчерпании вызовы не отправляются.
    ai_monthly_token_limit: int | None = Field(default=None, alias="AI_MONTHLY_TOKEN_LIMIT")
    # Профиль промпта (`ai.PROMPT_PROFILES`). Пусто — выбирается по AI_MODEL;
    # нужен, когда имя модели за прокси не совпадает с тем, под которое написан промпт.
    ai_prompt_profile: str = Field(default="", alias="AI_PROMPT_PROFILE")
    # Таймаут одного вызова классификации. Худший проход воркера 150 + 60 = 210 с
    # должен укладываться в `health.MAX_AGE_SECONDS["worker"] = 300`.
    ai_timeout_seconds: int = Field(default=60, alias="AI_TIMEOUT_SECONDS")
    # ── Пороги состояния «провайдер медленный / разметка отстаёт» (`services/ai_health.py`) ──
    # Медиана задержки последних вызовов выше порога — провайдер медленный.
    ai_slow_latency_seconds: int = Field(default=10, alias="AI_SLOW_LATENCY_SECONDS")
    # Самое старое сообщение без вердикта ждёт дольше порога — разметка отстаёт.
    # Порог меньше срока первого алерта (30 мин), иначе отставание успеет сдвинуть алерт.
    ai_slow_queue_minutes: int = Field(default=15, alias="AI_SLOW_QUEUE_MINUTES")
    # Прежние модели через запятую: их вердикты движок продолжает считать своими.
    # Без них смена AI_MODEL отправит всю прошлую разметку на переспрос.
    ai_previous_models: str = Field(default="", alias="AI_PREVIOUS_MODELS")

    # ── Резервная модель классификатора ────────────────────────
    # Пусто — резерва нет. Задано — при отказе основной (не-200, таймаут, сеть, пустой
    # или невалидный ответ) тот же вызов уходит на резервную модель с её промпт-профилем.
    ai_fallback_model: str = Field(default="", alias="AI_FALLBACK_MODEL")
    # Профиль промпта резерва. Пусто — по правилам `MODEL_PROFILE_RULES`.
    ai_fallback_prompt_profile: str = Field(
        default="", alias="AI_FALLBACK_PROMPT_PROFILE"
    )
    # Сколько отказов основной подряд переводят на резерв. Счётчик общий на процесс,
    # а не на сообщение: каждая попытка — отдельный тик воркера.
    ai_fallback_retries: int = Field(default=3, alias="AI_FALLBACK_RETRIES")
    # Окно, в котором отказы считаются идущими подряд.
    ai_fallback_retry_window_seconds: int = Field(
        default=300, alias="AI_FALLBACK_RETRY_WINDOW_SECONDS"
    )
    # Как часто в режиме резерва пробовать основную одним минимальным (платным) вызовом.
    ai_fallback_probe_seconds: int = Field(default=1800, alias="AI_FALLBACK_PROBE_SECONDS")
    # Сколько успешных проб подряд возвращают классификацию на основную.
    ai_fallback_restore_successes: int = Field(
        default=2, alias="AI_FALLBACK_RESTORE_SUCCESSES"
    )
    # Цены провайдера, ₽ за миллион токенов (вход/выход): расход в рублях на экране состояния.
    ai_price_in_per_m: float | None = Field(default=None, alias="AI_PRICE_IN_PER_M")
    ai_price_out_per_m: float | None = Field(default=None, alias="AI_PRICE_OUT_PER_M")

    @property
    def ai_accepted_models(self) -> list[str]:
        previous = [m.strip() for m in self.ai_previous_models.split(",") if m.strip()]
        # Резервная модель тоже «своя»: её вердикты пишутся в ту же таблицу.
        # Не требовать её в AI_PREVIOUS_MODELS: забытая строка всплывёт только при отказе основной.
        fallback = [self.ai_fallback_model.strip()] if self.ai_fallback_model.strip() else []
        ordered = previous + fallback
        seen = {self.ai_model}
        result = [self.ai_model]
        for model in ordered:
            if model not in seen:
                seen.add(model)
                result.append(model)
        return result

    # ── Приложение ─────────────────────────────────────────────
    tz: str = Field(default="Europe/Moscow", alias="TZ")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Метка окружения (dev/prod) для логов и экрана состояния.
    environment: str = Field(default="dev", alias="ENVIRONMENT")
    # Границы версий правил движка (docs/ARCHITECTURE.md, 7.3). Обращение, открытое до границы,
    # целиком живёт по старым правилам. Пусто — версия не включена. Включённую границу не сдвигать.
    episode_rules_v2_since: AwareDatetime | None = Field(
        default=None, alias="EPISODE_RULES_V2_SINCE"
    )
    episode_rules_v3_since: AwareDatetime | None = Field(
        default=None, alias="EPISODE_RULES_V3_SINCE"
    )
    # v4 включает правила v2 и v3.
    episode_rules_v4_since: AwareDatetime | None = Field(
        default=None, alias="EPISODE_RULES_V4_SINCE"
    )

    @field_validator(
        "bootstrap_owner_tg_id",
        "integrator_bot_id",
        "ai_monthly_token_limit",
        "ai_price_in_per_m",
        "ai_price_out_per_m",
        "notify_group_chat_id",
        "episode_rules_v2_since",
        "episode_rules_v3_since",
        "episode_rules_v4_since",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: Any) -> Any:
        """Пустая строка в .env (`KEY=`) означает «не задано», а не ошибку валидации."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "ai_fallback_retries",
        "ai_fallback_retry_window_seconds",
        "ai_fallback_probe_seconds",
        "ai_fallback_restore_successes",
        mode="before",
    )
    @classmethod
    def _empty_to_default_int(cls, value: Any, info: ValidationInfo) -> Any:
        """Пустая строка в .env даёт значение по умолчанию: эти поля не optional."""
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("allowed_updates")
    @classmethod
    def _strip_items(cls, value: str) -> str:
        return ",".join(part.strip() for part in value.split(",") if part.strip())

    @property
    def allowed_updates_list(self) -> list[str]:
        """Типы апдейтов для polling. Реакции (message_reaction*) Telegram без явной
        подписки не присылает; сейчас они не нужны.
        """
        return self.allowed_updates.split(",")

    def require_bot_token(self) -> str:
        if not self.bot_token:
            raise RuntimeError(
                "BOT_TOKEN пуст. Заполните .env — без токена бот не запустится."
            )
        return self.bot_token

    def require_database_url(self) -> str:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL пуст. Заполните .env — подключение к базе обязательно."
            )
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Номер группы уведомлений после миграции Telegram в супергруппу.
# Выставляется notify_group.record_migration/load_migration.
_notify_group_override: int | None = None


def set_notify_group_override(chat_id: int | None) -> None:
    global _notify_group_override
    _notify_group_override = chat_id


def effective_notify_group_id() -> int | None:
    """Действующий номер группы уведомлений: переезд важнее конфига."""
    return _notify_group_override or get_settings().notify_group_chat_id


def is_notify_group(tg_chat_id: Any) -> bool:
    """Это группа уведомлений? Для неё аналитика выключена полностью, иначе бот
    алертил бы о собственных сообщениях. Узнаётся и старый номер, и переехавший.
    """
    notify_id = get_settings().notify_group_chat_id
    if notify_id is not None and tg_chat_id == notify_id:
        return True
    return _notify_group_override is not None and tg_chat_id == _notify_group_override


def build_info() -> dict[str, str]:
    """Штамп сборки из app/BUILD_INFO (пишет scripts/deploy.sh, в git не хранится).
    Нет файла — пустой словарь.
    """
    from pathlib import Path

    path = Path(__file__).with_name("BUILD_INFO")
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


def notify_group_ids() -> set[int]:
    """Все известные номера группы уведомлений — для исключения запросом:
    история старого номера после переезда остаётся в базе.
    """
    ids = {get_settings().notify_group_chat_id, _notify_group_override}
    ids.discard(None)
    return {int(value) for value in ids}


def _runtime_database_url(self) -> str:
    """DSN для bot/worker: APP_DATABASE_URL (роль только с DML), если задан;
    иначе DATABASE_URL. DATABASE_URL (владелец схемы) остаётся у миграций и бэкапа.
    """
    return self.app_database_url or self.require_database_url()


Settings.runtime_database_url = _runtime_database_url  # type: ignore[attr-defined]
