"""Persistent session store backed by per-agent JSON files."""

import base64
import json
from pathlib import Path
from uuid import UUID

from substrat.persistence import atomic_write
from substrat.session.model import Session, SessionState

_SESSION_FILE = "session.json"


class SessionStore:
    """Thin I/O layer for session records. No in-memory cache."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def agent_dir(self, session_id: UUID) -> Path:
        """Return root/<uuid-hex>/ for the given session."""
        return self._root / session_id.hex

    def save(self, session: Session) -> None:
        """Serialize and atomically write session.json."""
        d = self.agent_dir(session.id)
        atomic_write(d / _SESSION_FILE, self._serialize(session))

    def load(self, session_id: UUID) -> Session:
        """Load one session record. Raises FileNotFoundError if missing."""
        path = self.agent_dir(session_id) / _SESSION_FILE
        return self._deserialize(path.read_bytes())

    def scan(self) -> list[Session]:
        """Load all session records under root."""
        if not self._root.is_dir():
            return []
        sessions: list[Session] = []
        for child in sorted(self._root.iterdir()):
            sf = child / _SESSION_FILE
            if sf.is_file():
                sessions.append(self._deserialize(sf.read_bytes()))
        return sessions

    def recover(self) -> list[Session]:
        """Startup recovery: flip ACTIVE -> SUSPENDED, re-save."""
        sessions = self.scan()
        for s in sessions:
            if s.state == SessionState.ACTIVE:
                s.transition(SessionState.SUSPENDED)
                self.save(s)
        return sessions

    @staticmethod
    def _serialize(session: Session) -> bytes:
        """Session -> JSON bytes."""
        obj = {
            "id": session.id.hex,
            "state": session.state.value,
            "provider_name": session.provider_name,
            "model": session.model,
            "created_at": session.created_at,
            "suspended_at": session.suspended_at,
            "provider_state": base64.b64encode(session.provider_state).decode(),
        }
        return json.dumps(obj, indent=2).encode()

    @staticmethod
    def _deserialize(data: bytes) -> Session:
        """JSON bytes -> Session."""
        obj = json.loads(data)
        return Session(
            id=UUID(obj["id"]),
            state=SessionState(obj["state"]),
            provider_name=obj["provider_name"],
            model=obj["model"],
            created_at=obj["created_at"],
            suspended_at=obj["suspended_at"],
            provider_state=base64.b64decode(obj["provider_state"]),
        )
