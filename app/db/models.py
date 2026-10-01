"""Модель данных.

Сырьё отделено от интерпретации (docs/ARCHITECTURE.md, раздел 1): telegram_update —
неизменяемый журнал, всё производное (message, attribution, обращения, вердикты)
можно снести и пересчитать.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ═══════════════════════════════════════════════════════════════
# Перечисления
# ═══════════════════════════════════════════════════════════════


class TransportActorKind(str, enum.Enum):
    """Кто прислал сообщение — только по Telegram ID и типу апдейта, не по тексту:
    ошибка парсера ФИО не должна ломать разделение сторон.
    """

    HUMAN_USER = "human_user"
    INTEGRATOR_BOT = "integrator_bot"
    OTHER_BOT = "other_bot"
    TELEGRAM_SYSTEM = "telegram_system"


class BusinessSide(str, enum.Enum):
    """Чья это сторона — производное значение, пересчитывается вместе с парсером."""

    CLIENT = "client"
    COMPANY = "company"
    INTEGRATOR_SYSTEM = "integrator_system"
    UNKNOWN = "unknown"


class ChatState(str, enum.Enum):
    DISCOVERED = "discovered"  # бот в чате, но в отчёты не идёт
    TRACKED = "tracked"
    PAUSED = "paused"
    ARCHIVED = "archived"  # бота удалили; история сохраняется


class AttributionMethod(str, enum.Enum):
    EXACT = "exact"
    TG_ID = "tg_id"
    FUZZY = "fuzzy"
    AI = "ai"
    CONTINUATION = "continuation"
    MANUAL = "manual"


class SenderRuleKind(str, enum.Enum):
    """Вид отправителя в решении человека. Человек и чужой бот узнаются по Telegram ID
    во всех чатах; «от имени группы» — одна служебная учётка на весь Telegram,
    поэтому её решение действует в одном чате.
    """

    TG_USER = "tg_user"
    BOT = "bot"
    ANONYMOUS_ADMIN = "anonymous_admin"


class SenderRuleSide(str, enum.Enum):
    """Сторона, выбранная человеком. Без `unknown`: решение человека его не создаёт.
    `system` раскрывается в `BusinessSide.INTEGRATOR_SYSTEM` (вне обеих сторон).
    """

    COMPANY = "company"
    CLIENT = "client"
    SYSTEM = "system"


class BotRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"


class BotUserState(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


# Типы PostgreSQL создаются один раз и переиспользуются: иначе общий тип
# (например, BotRole в bot_user и invite_code) даст повторный CREATE TYPE.
transport_actor_kind_type = Enum(TransportActorKind, name="transport_actor_kind")
business_side_type = Enum(BusinessSide, name="business_side")
chat_state_type = Enum(ChatState, name="chat_state")
attribution_method_type = Enum(AttributionMethod, name="attribution_method")
bot_role_type = Enum(BotRole, name="bot_role")
bot_user_state_type = Enum(BotUserState, name="bot_user_state")
sender_rule_kind_type = Enum(SenderRuleKind, name="sender_rule_kind")
sender_rule_side_type = Enum(SenderRuleSide, name="sender_rule_side")


# ═══════════════════════════════════════════════════════════════
# Сырьё
# ═══════════════════════════════════════════════════════════════


class TelegramUpdate(Base):
    """Неизменяемый журнал апдейтов. update_id как первичный ключ даёт
    идемпотентность для всех типов событий при повторной доставке.
    """

    __tablename__ = "telegram_update"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    update_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = _now()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_tg_update_unprocessed", "received_at", postgresql_where=sa_text("processed_at IS NULL")),
    )


# ═══════════════════════════════════════════════════════════════
# Чаты
# ═══════════════════════════════════════════════════════════════


class Chat(Base):
    __tablename__ = "chat"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    state: Mapped[ChatState] = mapped_column(
        chat_state_type, default=ChatState.DISCOVERED, nullable=False
    )
    is_forum: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Зарезервировано; сейчас не используется.
    client_name: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str | None] = mapped_column(String(64))

    # С какого момента чат отслеживается: отчёт отличает «ноль сообщений»
    # от «ещё не подключён».
    tracked_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Зарезервировано; сейчас не используется.
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _now()

    # passive_deletes: каскад держит база (ON DELETE CASCADE). Без этого ORM
    # зануляет message.chat_id (NOT NULL), и удаление чата падает.
    messages: Mapped[list[Message]] = relationship(
        back_populates="chat", passive_deletes=True
    )


class ChatTrackingPeriod(Base):
    """Интервалы, когда чат был в анализе (паузы и возвраты). Отчёты за прошлое
    смотрят на них, а не на сегодняшнее состояние чата. `ended_at IS NULL` — наблюдается сейчас.
    """

    __tablename__ = "chat_tracking_period"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_tracking_period_chat", "chat_id", "started_at"),
        # Один открытый интервал на чат — инвариант держит база, а не проверка перед вставкой.
        Index(
            "uq_tracking_period_open",
            "chat_id",
            unique=True,
            postgresql_where=sa_text("ended_at IS NULL"),
        ),
    )


class ChatTitleHistory(Base):
    """История названий: чат переименовали, а искать его владелец будет по имени."""

    __tablename__ = "chat_title_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    changed_at: Mapped[datetime] = _now()


# ═══════════════════════════════════════════════════════════════
# Сообщения
# ═══════════════════════════════════════════════════════════════


class Message(Base):
    """Проекция актуального состояния сообщения. Правка обновляет текст здесь,
    апдейт в журнале остаётся нетронутым.
    """

    __tablename__ = "message"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"), nullable=False)

    # Ветка форума: без неё параллельные топики склеятся в один поток.
    thread_id: Mapped[int | None] = mapped_column(BigInteger)

    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger)

    transport_actor_kind: Mapped[TransportActorKind] = mapped_column(
        transport_actor_kind_type, nullable=False
    )
    business_side: Mapped[BusinessSide] = mapped_column(
        business_side_type, default=BusinessSide.UNKNOWN, nullable=False
    )
    # Версия правила стороны (ingestion.SIDE_RULE_VERSION), по которой посчитана business_side.
    side_rule_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    text: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list | None] = mapped_column(JSONB)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    media_kind: Mapped[str | None] = mapped_column(String(32))
    has_media: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    reply_to_tg_message_id: Mapped[int | None] = mapped_column(BigInteger)

    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Правка могла превратить благодарность в новую просьбу — нужна переклассификация.
    needs_reclassification: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = _now()

    chat: Mapped[Chat] = relationship(back_populates="messages")
    attribution: Mapped[Attribution | None] = relationship(
        back_populates="message", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("chat_id", "tg_message_id", name="uq_message_chat_tgid"),
        Index("ix_message_chat_thread_sent", "chat_id", "thread_id", "sent_at"),
        Index("ix_message_side_sent", "business_side", "sent_at"),
        Index(
            "ix_message_needs_reclass",
            "chat_id",
            postgresql_where=sa_text("needs_reclassification"),
        ),
    )


# ═══════════════════════════════════════════════════════════════
# Сотрудники и атрибуция
# ═══════════════════════════════════════════════════════════════


class Staff(Base):
    """Справочник сотрудников. Без привязки к чатам: связь «сотрудник — чат»
    вычисляется из фактических сообщений.
    """

    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Обязателен при совпадении normalized_name, иначе однофамильцы схлопнутся в профиль.
    discriminator: Mapped[str | None] = mapped_column(String(255))

    # Отдельная таблица: уникальность алиаса держит индекс. selectin — чтобы
    # staff.aliases не уходил в ленивую загрузку внутри async-сессии.
    aliases: Mapped[list["StaffAlias"]] = relationship(
        back_populates="staff",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)

    # Зарезервировано под период работы сотрудника; сейчас не используется.
    # Тёзок разводит `discriminator`, а не период.
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # manager — помощник (реагирует первым и передаёт), specialist — отвечает по существу.
    # Выводится из наблюдения за передачами (staff_roles.refresh_observed_roles) или правится
    # вручную; NULL — не определена. role_source: auto | manual, ручную автоматика не трогает.
    role: Mapped[str | None] = mapped_column(String(16))
    role_source: Mapped[str | None] = mapped_column(String(8))

    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        # nulls_not_distinct: иначе два одинаковых имени без различителя пройдут ограничение.
        UniqueConstraint(
            "normalized_name",
            "discriminator",
            name="uq_staff_name_disc",
            postgresql_nulls_not_distinct=True,
        ),
    )


class StaffAlias(Base):
    """Дополнительное написание имени сотрудника; alias хранится нормализованным.
    Уникальный индекс по alias не даёт двум людям владеть одним написанием.
    """

    __tablename__ = "staff_alias"

    id: Mapped[int] = mapped_column(primary_key=True)
    staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff.id", ondelete="CASCADE"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = _now()

    staff: Mapped[Staff] = relationship(back_populates="aliases")


class Attribution(Base):
    """Кто автор сообщения; производное, пересчитывается при смене парсера.
    staff_id = NULL — автор не определён: в метриках чата учитывается, в метриках сотрудника нет.
    """

    __tablename__ = "attribution"

    message_id: Mapped[int] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), primary_key=True
    )
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    method: Mapped[AttributionMethod | None] = mapped_column(
        attribution_method_type
    )
    confidence: Mapped[float | None] = mapped_column()
    # Версия парсера (attribution.PARSER_VERSION): отстающие строки пересчитываются.
    parser_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    raw_name: Mapped[str | None] = mapped_column(String(255))

    # Кто разметил вручную: владелец видит, какая часть цифр создана рукой.
    assigned_by: Mapped[int | None] = mapped_column(ForeignKey("bot_user.id", ondelete="SET NULL"))
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    message: Mapped[Message] = relationship(back_populates="attribution")

    __table_args__ = (
        Index("ix_attribution_staff", "staff_id", postgresql_where=sa_text("staff_id IS NOT NULL")),
        Index("ix_attribution_unresolved", "parser_version", postgresql_where=sa_text("staff_id IS NULL")),
    )


class SenderRule(Base):
    """«Кто это» про отправителя — решение человека; действует на всю его переписку.

    Это данные, а не версия кода: пересчёт по `SIDE_RULE_VERSION` решение не затирает,
    он идёт через `resolve_business_side`, которая правило и читает.
    `key` — Telegram ID отправителя; `chat_id` заполнен только у `anonymous_admin`.
    Уникальность `NULLS NOT DISTINCT`, иначе два правила на одного человека.
    `display` — имя на момент решения: в `message` имени отправителя нет.
    """

    __tablename__ = "sender_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SenderRuleKind] = mapped_column(sender_rule_kind_type, nullable=False)
    key: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"))

    side: Mapped[SenderRuleSide] = mapped_column(sender_rule_side_type, nullable=False)
    # Только при side = company.
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))

    decided_by: Mapped[int | None] = mapped_column(
        ForeignKey("bot_user.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime] = _now()
    note: Mapped[str | None] = mapped_column(Text)
    display: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        UniqueConstraint(
            "kind",
            "key",
            "chat_id",
            name="uq_sender_rule",
            postgresql_nulls_not_distinct=True,
        ),
    )


# ═══════════════════════════════════════════════════════════════
# Пользователи бота
# ═══════════════════════════════════════════════════════════════


class BotUser(Base):
    __tablename__ = "bot_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(255))

    role: Mapped[BotRole] = mapped_column(bot_role_type, nullable=False)
    permissions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    state: Mapped[BotUserState] = mapped_column(
        bot_user_state_type, default=BotUserState.PENDING, nullable=False
    )

    # Привязка учётки manager к справочнику: чьи показатели показывать.
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))

    invited_by: Mapped[int | None] = mapped_column(ForeignKey("bot_user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now()

    # Уведомления в личку. Выключить можно только при включённой группе уведомлений;
    # системные уведомления (заявки, сбой ИИ) флаг не трогает.
    notify_personal: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )

    __table_args__ = (
        # Один сотрудник — одна учётка (частичный индекс: NULL повторяется).
        # Проверка в коде — только ради понятной ошибки, инвариант держит база.
        Index(
            "uq_bot_user_staff",
            "staff_id",
            unique=True,
            postgresql_where=sa_text("staff_id IS NOT NULL"),
        ),
    )


class InviteCode(Base):
    """Одноразовое приглашение; в базе только хеш кода. sha256 без соли намеренно:
    код — 128 случайных бит, а быстрый хеш оставляет погашение одним условным UPDATE.
    """

    __tablename__ = "invite_code"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    role: Mapped[BotRole] = mapped_column(bot_role_type, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("bot_user.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[int | None] = mapped_column(ForeignKey("bot_user.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class AuditLog(Base):
    """Действия, влияющие на чужие данные или на видимость проблем (например, пауза чата)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("bot_user.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(64))
    object_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (Index("ix_audit_created", "created_at"),)


# ═══════════════════════════════════════════════════════════════
# Настройки
# ═══════════════════════════════════════════════════════════════


class Setting(Base):
    """Бизнес-настройки, которые владелец меняет кнопками. Секреты — только в окружении."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("bot_user.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ═══════════════════════════════════════════════════════════════
# Эпизоды и классификация
# ═══════════════════════════════════════════════════════════════


class InteractionState(str, enum.Enum):
    OPEN = "open"  # обращение есть, реакции нет
    REACTED = "reacted"  # была первая реакция, ответа по существу нет
    ANSWERED = "answered"  # закрыт содержательным ответом
    NO_RESPONSE_NEEDED = "no_response_needed"  # ИИ решил, что ответ не требуется
    ABANDONED = "abandoned"  # превышены пределы эпизода


interaction_state_type = Enum(InteractionState, name="interaction_state")


class Interaction(Base):
    """Обращение клиента и всё, что было ответом. Производное: пересобирается
    из сообщений на каждом проходе воркера.
    """

    __tablename__ = "interaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"), nullable=False)
    thread_id: Mapped[int | None] = mapped_column(BigInteger)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_by_message_id: Mapped[int] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), nullable=False
    )
    last_client_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_messages: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    first_reaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_reaction_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff.id", ondelete="SET NULL")
    )
    # Какое сообщение стало реакцией/ответом: время неоднозначно (два сообщения в одну секунду).
    first_reaction_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("message.id", ondelete="SET NULL")
    )
    substantive_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("message.id", ondelete="SET NULL")
    )
    substantive_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    substantive_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff.id", ondelete="SET NULL")
    )

    # Момент передачи специалисту; с него идёт второй слой срока. Пусто — передачи
    # не было, второго слоя нет.
    handoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handoff_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff.id", ondelete="SET NULL")
    )

    state: Mapped[InteractionState] = mapped_column(
        interaction_state_type, default=InteractionState.OPEN, nullable=False
    )

    # Календарная и рабочая (*_business_*) скорость хранятся обе.
    # ttfr — до первой реакции, ttfa — до ответа по существу.
    ttfr_seconds: Mapped[int | None] = mapped_column(BigInteger)
    ttfr_business_seconds: Mapped[int | None] = mapped_column(BigInteger)
    ttfa_seconds: Mapped[int | None] = mapped_column(BigInteger)
    ttfa_business_seconds: Mapped[int | None] = mapped_column(BigInteger)

    # Просрочка по сроку ответа (calendar.response_deadline). Хранится, чтобы отчёт
    # и алерты не расходились и SQL не повторял правило календаря.
    sla_breached: Mapped[bool | None] = mapped_column(Boolean)
    # Просрочка второго слоя: ответ специалиста после передачи.
    substantive_breached: Mapped[bool | None] = mapped_column(Boolean)

    # Версия правил движка (v1–v4) по моменту открытия, см. config.EPISODE_RULES_V*_SINCE.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        Index("ix_interaction_chat_opened", "chat_id", "thread_id", "opened_at"),
        Index(
            "ix_interaction_active",
            "state",
            postgresql_where=sa_text("state IN ('OPEN', 'REACTED')"),
        ),
    )


class Classification(Base):
    """Вердикт ИИ по одному сообщению. Уникальность включает модель: вердикты
    нескольких моделей по одним сообщениям хранятся рядом.
    """

    __tablename__ = "classification"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # "rule" — детерминированное правило («спасибо», «ок»), "model" — языковая модель.
    # В тихом режиме движок учитывает только правила.
    source: Mapped[str] = mapped_column(
        String(16), default="model", server_default="model", nullable=False
    )

    label: Mapped[str | None] = mapped_column(String(32))
    requires_response: Mapped[bool | None] = mapped_column(Boolean)
    is_substantive: Mapped[bool | None] = mapped_column(Boolean)

    # `message.id` открывающего сообщения обращения, которое закрывает ответ; NULL —
    # связь неизвестна. Внешнего ключа нет намеренно: обращения пересобираются каждый тик.
    answers_request_id: Mapped[int | None] = mapped_column(BigInteger)

    confidence: Mapped[float | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)

    # Счёт отказов подряд; значим только у строки с `error` (пауза и предел повторов).
    # Строка отказа одна и переписывается с переносом счёта.
    attempts: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )

    created_at: Mapped[datetime] = _now()

    __table_args__ = (
        UniqueConstraint("message_id", "model", "prompt_version", name="uq_classification_msg"),
        Index("ix_classification_message", "message_id"),
    )


class AiUsage(Base):
    """Расход токенов по дням и моделям: прозрачность и месячный потолок."""

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    requests: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("day", "model", name="uq_ai_usage_day_model"),)


class BreachDismissal(Base):
    """Решение руководителя «снять нарушение»: снимает обращение из списков и счётчиков
    нарушений, само обращение остаётся. Ключ — (чат, открывающее сообщение):
    id обращений меняются при пересборке. Отменяется с того же экрана.
    """

    __tablename__ = "breach_dismissal"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"), nullable=False)
    opened_by_message_id: Mapped[int] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), nullable=False
    )
    dismissed_by: Mapped[int | None] = mapped_column(
        ForeignKey("bot_user.id", ondelete="SET NULL")
    )
    dismissed_at: Mapped[datetime] = _now()

    __table_args__ = (
        UniqueConstraint("chat_id", "opened_by_message_id", name="uq_breach_dismissal"),
    )


class AlertLog(Base):
    """Журнал алертов: дедупликация и история. Ключ — (чат, открывающее сообщение,
    вид алерта): id обращений меняются при пересборке.
    """

    __tablename__ = "alert_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chat.id", ondelete="CASCADE"), nullable=False)
    opened_by_message_id: Mapped[int] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    recipients: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    sent_at: Mapped[datetime] = _now()

    # Всегда false: теневых срабатываний система не создаёт; колонка сохранена ради
    # совместимости схемы.
    shadow: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # Запись создаётся до отправки (сбой между отправкой и записью задвоил бы алерт),
    # но событие не закрыто, пока доставки не было: следующий тик повторит попытку.
    delivered: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)

    # Сообщения алерта для зачёркивания: {"<tg_id>": message_id}, ключи JSONB — строки.
    # Текст хранится таким, каким ушёл: к закрытию состояние обращения уже другое.
    message_ids: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=sa_text("'{}'::jsonb"), nullable=False
    )
    sent_text: Mapped[str | None] = mapped_column(Text)
    # Ставится и при отказе Telegram, иначе каждый тик пытался бы править снова.
    struck_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Покрытие: по чату не больше одного открытого алерта каждого слоя. Покрытая
    # просрочка пишется в журнал без получателей и своего сообщения — для сводки и метрик.
    covered_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_log.id", ondelete="SET NULL"), index=True
    )

    __table_args__ = (
        UniqueConstraint("chat_id", "opened_by_message_id", "kind", name="uq_alert_once"),
    )
