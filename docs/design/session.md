# Session Model

Sessions are the lowest layer — the substrate under the agent hierarchy and
messaging. A session is a 1:1 wrapper around a single provider instance for a
single agent. Sessions handle provider lifecycle and context persistence only.
They know nothing about agent trees or messages.

## Data Model

```python
class SessionState(enum.Enum):
    CREATED = "created"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"

@dataclass
class Session:
    id: UUID = field(default_factory=uuid4)
    state: SessionState = SessionState.CREATED
    provider_name: str = "cursor-agent"
    model: str = ""
    created_at: str = ""           # ISO 8601.
    suspended_at: str | None = None
    provider_state: bytes = b""    # Opaque blob from ProviderSession.suspend().
```

## State Machine

```
CREATED → ACTIVE → SUSPENDED → ACTIVE  (cycle)
                 → TERMINATED
           ACTIVE → TERMINATED
```

Transitions are validated; invalid transitions raise `SessionStateError`.

## Persistence

Each session persists to `~/.substrat/sessions/<uuid>/state.json` as JSON.
`provider_state` is base64-encoded. On daemon startup, sessions found in
`ACTIVE` state are moved to `SUSPENDED` (daemon was not running, provider is
dead).

## Multiplexing

Limited number of active slots (configurable, default 4). Slots represent
concurrently running `ProviderSession` instances, keyed by session ID. When all
slots are full, the LRU session is suspended to free one.

```python
class SessionMultiplexer:
    def __init__(self, max_slots: int = 4) -> None:
        self._slots: dict[UUID, ProviderSession] = {}
        self._lru: list[UUID] = []

    async def acquire(self, session_id: UUID) -> ProviderSession: ...
    async def release(self, session_id: UUID) -> None: ...
```
