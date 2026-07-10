"""Fixtures compartidos entre tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from memex.core.models import Chunk, Conversation, Message, Project, Sender, Source
from memex.core.storage.db import connect_and_init


@pytest.fixture(autouse=True)
def _isolate_machine_state(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ningun test puede leer o escribir el estado real de la maquina de dev.

    `settings.db_path` (y todo lo derivado de su carpeta: `sync_state.json`,
    peers, history, token) apunta a un tmp por test. Sin esto, un dev que activo
    sync de verdad (gate ON + peers reales) rompe los tests de defaults de
    `serve` y hace que cada lifespan de TestClient arranque el auto-sync real
    contra peers apagados (timeouts de red por test). Un test que necesita una
    ruta concreta la sigue pisando con su propio monkeypatch.
    """
    from memex.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path_factory.mktemp("machine-state") / "memex.db")


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Conexión SQLite in-memory con sqlite-vec cargado y schema aplicado."""
    conn = connect_and_init(":memory:")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def project() -> Project:
    return Project(
        uuid="proj-0001-aaaa",
        name="Memex",
        description="Proyecto de prueba",
        prompt_template="Sos un asistente útil.",
        is_private=True,
        is_starter_project=False,
        creator_uuid="user-0001",
        creator_name="Dionisio",
        created_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        updated_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
    )


@pytest.fixture
def conversation(project: Project) -> Conversation:
    return Conversation(
        uuid="conv-0001",
        title="Charla de arranque",
        summary="Resumen de la conversación.",
        source=Source.DESIGN_CHAT,
        project_uuid=project.uuid,
        account_uuid="acct-0001",
        created_at=datetime(2026, 3, 5, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 3, 5, 10, 0, tzinfo=UTC),
    )


@pytest.fixture
def human_message(conversation: Conversation) -> Message:
    return Message(
        uuid="msg-0001",
        conversation_uuid=conversation.uuid,
        parent_uuid=None,
        sender=Sender.HUMAN,
        text="¿Cómo arranco con sqlite-vec?",
        raw_content=None,
        has_tool_use=False,
        has_attachments=False,
        created_at=datetime(2026, 3, 5, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 3, 5, 9, 0, tzinfo=UTC),
    )


@pytest.fixture
def assistant_message(conversation: Conversation, human_message: Message) -> Message:
    return Message(
        uuid="msg-0002",
        conversation_uuid=conversation.uuid,
        parent_uuid=human_message.uuid,
        sender=Sender.ASSISTANT,
        text="Importás la lib, cargás la extensión y creás la virtual table.",
        raw_content=[
            {
                "type": "text",
                "text": "Importás la lib, cargás la extensión y creás la virtual table.",
            }
        ],
        has_tool_use=False,
        has_attachments=False,
        created_at=datetime(2026, 3, 5, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 3, 5, 9, 1, tzinfo=UTC),
    )


@pytest.fixture
def chunk(conversation: Conversation) -> Chunk:
    """Chunk standalone, sin referencia a un mensaje específico.

    Para tests que ejerciten el FK chunks.message_uuid, ver `chunk_with_message`.
    """
    return Chunk(
        id=None,
        conversation_uuid=conversation.uuid,
        message_uuid=None,
        sender=Sender.ASSISTANT.value,
        text="Importás la lib, cargás la extensión y creás la virtual table.",
        char_start=0,
        char_end=63,
        created_at=datetime(2026, 3, 5, 9, 1, tzinfo=UTC),
    )


@pytest.fixture
def chunk_with_message(conversation: Conversation, assistant_message: Message) -> Chunk:
    return Chunk(
        id=None,
        conversation_uuid=conversation.uuid,
        message_uuid=assistant_message.uuid,
        sender=Sender.ASSISTANT.value,
        text=assistant_message.text,
        char_start=0,
        char_end=len(assistant_message.text),
        created_at=assistant_message.created_at,
    )
