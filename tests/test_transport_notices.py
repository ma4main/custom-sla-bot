import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.models import BusinessSide, Message, TransportActorKind
from app.services.transport_notices import is_integrator_notice


@pytest.mark.parametrize('text', [
    '@GroupAnonymousBot \n\nID этого телеграм чата = -1001000000001\n\nВы не авторизованы',
    'Вы не авторизованы! Воспользуйтесь командой /auth',
    '@GroupAnonymousBot Вы авторизованы на corp.example.com как Анна Иванова',
    '@demo_vera Вы авторизованы на corp.example.com как Анна Иванова',
    'Если хотите переавторизоваться отправьте команду /auth с указанием портала',
    'Вы уже привязаны к этому чату!',
    'Вы успешно привязаны к другому чату',
    'Пересылка сообщений ИЗ этого чата В другие привязанные чаты ВКЛЮЧЕНА.\n'
    'Чтобы прекратить пересылку, используйте команду "/mute_outgoing_messages".',
    'Пересылка сообщений ИЗ этого чата В другие привязанные чаты ОСТАНОВЛЕНА.\n'
    'Чтобы возобновить пересылку, используйте команду "/mute_outgoing_messages 0".',
    'Чат настроен',
])
def test_machine_reply_is_not_manager_reaction(text):
    message = Message(text=text, transport_actor_kind=TransportActorKind.INTEGRATOR_BOT,
                      business_side=BusinessSide.COMPANY)
    assert is_integrator_notice(message)


@pytest.mark.parametrize('text,actor,side', [
    ('Вы не авторизованы', TransportActorKind.HUMAN_USER, BusinessSide.COMPANY),
    ('Анна Иванова [portal.example] пишет:\nВы не авторизованы',
     TransportActorKind.INTEGRATOR_BOT, BusinessSide.COMPANY),
    ('Вы не авторизованы', TransportActorKind.INTEGRATOR_BOT, BusinessSide.CLIENT),
    ('Вы не авторизованы, направила запрос в поддержку',
     TransportActorKind.INTEGRATOR_BOT, BusinessSide.COMPANY),
    ('Анна Иванова [portal.example] пишет:\nРабота чата восстановлена',
     TransportActorKind.INTEGRATOR_BOT, BusinessSide.COMPANY),
    ('Работа чата восстановлена', TransportActorKind.INTEGRATOR_BOT, BusinessSide.COMPANY),
    (None, TransportActorKind.INTEGRATOR_BOT, BusinessSide.COMPANY),
])
def test_human_or_unknown_text_is_not_suppressed(text, actor, side):
    assert not is_integrator_notice(Message(text=text, transport_actor_kind=actor, business_side=side))


def test_cutover_requires_timezone_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("EPISODE_RULES_V2_SINCE", raising=False)
    assert Settings(_env_file=None, EPISODE_RULES_V2_SINCE='').episode_rules_v2_since is None
    assert Settings(_env_file=None).episode_rules_v2_since is None
    since = Settings(_env_file=None, EPISODE_RULES_V2_SINCE='2026-09-15T10:00:00+03:00').episode_rules_v2_since
    assert since is not None and since.utcoffset() is not None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, EPISODE_RULES_V2_SINCE='2026-09-14T10:00:00')
