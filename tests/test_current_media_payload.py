"""Маркер вложения текущего сообщения совпадает с представлением в хвосте."""

from app.services.ai import current_message_text
from tests.conftest import requires_db


def test_current_document_keeps_its_caption_after_the_marker():
    assert current_message_text(
        "дополнительное соглашение", has_media=True, media_kind="document"
    ) == "[документ]: дополнительное соглашение"


def test_current_media_without_caption_uses_only_a_generic_marker():
    assert current_message_text(None, has_media=True, media_kind="photo") == "[фото]"
    assert current_message_text("", has_media=True, media_kind="unrecognised") == "[файл]"


def test_no_media_keeps_ordinary_text_verbatim():
    assert current_message_text("  обычный текст  ") == "  обычный текст  "


def test_media_kind_from_a_partial_snapshot_still_matches_tail_rendering():
    assert current_message_text("подпись", has_media=False, media_kind="document") == (
        "[документ]: подпись"
    )


def test_current_company_document_uses_the_same_representation():
    assert current_message_text(None, has_media=True, media_kind="document") == "[документ]"


@requires_db
async def test_worker_uses_current_company_media_metadata(session, monkeypatch):
    """Воркер не теряет вложение текущего сообщения сотрудника вне хвоста."""
    from contextlib import asynccontextmanager

    from app.config import get_settings
    from app.db.models import BusinessSide
    from app.services.tracking import open_period
    from app.worker import main as worker
    from tests.test_classify_context_v11 import NOW, _chat, _message

    monkeypatch.setattr(get_settings(), "ai_shadow_mode", False)
    chat = await _chat(session, -100900091, "Текущий файл сотрудника")
    await open_period(session, chat, reason="test", at=NOW)
    await _message(
        session, chat, 1, side=BusinessSide.COMPANY, text="", media_kind="document", minutes=1
    )
    payloads: list[str] = []

    @asynccontextmanager
    async def local_scope():
        yield session
        await session.flush()

    monkeypatch.setattr(worker, "session_scope", local_scope)

    class Recorder(worker.AiClient):
        async def classify_detailed(self, system, payload, **kwargs):
            payloads.append(payload)
            return {
                "error": None,
                "usage": (0, 0),
                "verdict": {
                    "label": "substantive",
                    "is_substantive": True,
                    "answers_request_id": None,
                },
            }

    await worker.classify_pending(Recorder())
    assert payloads == [
        "=== ПРЕДЫДУЩИЕ СООБЩЕНИЯ ===\n(нет)\n"
        "=== ОТКРЫТЫЕ ОБРАЩЕНИЯ ===\n(нет)\n"
        "=== СООБЩЕНИЕ СОТРУДНИКА ===\n[документ]\n==="
    ]
