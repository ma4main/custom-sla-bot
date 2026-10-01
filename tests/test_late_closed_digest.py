"""Поздний ответ между тиками алертов попадает в следующую сводку ровно один раз."""
from datetime import timedelta
import pytest
from sqlalchemy import func,select
from app.db.models import AlertLog,Classification,Interaction,InteractionState,Message,BusinessSide,TransportActorKind,Setting
from app.config import get_settings
from app.services import alert_digest
from app.services.episodes import rebuild_interactions
from tests.conftest import requires_db
from tests.test_episodes_dry_run import _chat_with_request,OPENED_AT


def freeze(monkeypatch):
    class Clock:
        @staticmethod
        def now(tz=None):return OPENED_AT+timedelta(hours=2)
    monkeypatch.setattr(alert_digest,'datetime',Clock)


@requires_db
async def test_rebuild_late_reply_before_alert_tick_is_in_next_digest_once(session,monkeypatch):
    chat,old=await _chat_with_request(session)
    answer_at=OPENED_AT+timedelta(minutes=40)
    answer=Message(chat_id=chat.id,tg_message_id=2,transport_actor_kind=TransportActorKind.HUMAN_USER,
                   business_side=BusinessSide.COMPANY,text='Счёт готов',sent_at=answer_at)
    session.add(answer);await session.flush()
    session.add(Classification(message_id=answer.id,model=get_settings().ai_model,prompt_version=9,
                               label='substantive',is_substantive=True))
    await session.flush()
    await rebuild_interactions(session,now=answer_at+timedelta(minutes=1));await session.flush()
    case=await session.scalar(select(Interaction).where(Interaction.chat_id==chat.id))
    assert case.sla_breached and case.first_reaction_at==answer_at
    freeze(monkeypatch)
    view=await alert_digest.build_alert_digest(session,'13:00',since=OPENED_AT)
    assert view and chat.title in view.text and 'ответ с опозданием' in view.text
    assert 'алерт сработал' not in view.text
    assert await session.scalar(select(func.count()).select_from(AlertLog))==0
    assert await alert_digest.build_alert_digest(session,'16:40',since=answer_at+timedelta(seconds=1)) is None


@requires_db
@pytest.mark.parametrize('delivered',[True,False])
async def test_existing_alert_does_not_duplicate_late_closure(session,monkeypatch,delivered):
    chat,old=await _chat_with_request(session)
    case=Interaction(chat_id=chat.id,opened_by_message_id=old.id,opened_at=old.sent_at,last_client_at=old.sent_at,
                     client_messages=1,version=1,first_reaction_at=OPENED_AT+timedelta(minutes=40),
                     state=InteractionState.ANSWERED,sla_breached=True,ttfr_seconds=2400,ttfr_business_seconds=2400)
    session.add(case)
    session.add(AlertLog(chat_id=chat.id,opened_by_message_id=old.id,kind='no_reaction',
                         sent_at=OPENED_AT+timedelta(minutes=31),recipients=[],delivered=delivered,shadow=False))
    await session.flush();freeze(monkeypatch)
    view=await alert_digest.build_alert_digest(session,'13:00',since=OPENED_AT)
    assert view and view.text.count(chat.title)==1
    assert 'алерт сработал' in view.text
    stored=await session.scalar(select(AlertLog).where(AlertLog.opened_by_message_id==old.id))
    assert stored.delivered is delivered
    assert await session.scalar(select(func.count()).select_from(AlertLog))==1


@requires_db
async def test_timely_reply_without_alert_is_not_a_breach_summary(session,monkeypatch):
    chat,old=await _chat_with_request(session)
    session.add(Interaction(chat_id=chat.id,opened_by_message_id=old.id,opened_at=old.sent_at,last_client_at=old.sent_at,
                client_messages=1,version=1,first_reaction_at=OPENED_AT+timedelta(minutes=10),
                state=InteractionState.ANSWERED,sla_breached=False,ttfr_seconds=600,ttfr_business_seconds=600))
    await session.flush();freeze(monkeypatch)
    assert await alert_digest.build_alert_digest(session,'13:00',since=OPENED_AT) is None


@requires_db
@pytest.mark.parametrize('mode',['on','off','shadow'])
async def test_specialist_late_closure_respects_enabled_mode(session,monkeypatch,mode):
    chat,old=await _chat_with_request(session)
    session.add(Setting(key='alerts',value={'substantive_mode':mode}))
    closed_at=OPENED_AT+timedelta(days=1,minutes=40)
    session.add(Interaction(chat_id=chat.id,opened_by_message_id=old.id,opened_at=old.sent_at,last_client_at=old.sent_at,
                client_messages=1,version=1,first_reaction_at=OPENED_AT+timedelta(minutes=5),
                handoff_at=OPENED_AT+timedelta(minutes=5),substantive_at=closed_at,
                state=InteractionState.ANSWERED,sla_breached=False,substantive_breached=True,
                ttfr_seconds=300,ttfr_business_seconds=300,ttfa_seconds=88800,ttfa_business_seconds=30000))
    await session.flush()
    class Clock:
        @staticmethod
        def now(tz=None):return closed_at+timedelta(hours=1)
    monkeypatch.setattr(alert_digest,'datetime',Clock)
    view=await alert_digest.build_alert_digest(session,'13:00',since=closed_at-timedelta(minutes=1))
    if mode=='on':assert view and 'ответ с опозданием' in view.text
    else:assert view is None
    assert await session.scalar(select(func.count()).select_from(AlertLog))==0
