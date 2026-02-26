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
    provider_name: str = ""
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

Two layers:

### Session record

Metadata snapshot at `~/.substrat/agents/<uuid>/session.json`. Contains id,
state, provider_name, model, timestamps. Written atomically (temp + fsync +
rename) on creation and state transitions.

The `provider_state` blob is captured at `suspend()` time and stored in this
file (base64-encoded). It is a **performance optimization** for fast restore
(avoids replaying the log), not a correctness requirement.

### Event log

Append-only JSONL at `~/.substrat/agents/<uuid>/events.jsonl`. Logs every
`send()` prompt and response, plus state transitions. This is the **source of
truth** for crash recovery — the full conversation can be reconstructed from
it. See `crash_recovery.md` for details.

On daemon startup, sessions found in `ACTIVE` state are moved to `SUSPENDED`
(daemon was not running, provider is dead).

## Serialization contract

All data that enters the event log must be JSON-serializable from plain
types: strings, numbers, bools, lists, dicts. No opaque Python objects, no
datetimes-as-objects, no UUIDs-as-objects. UUIDs are stored as hex strings,
timestamps as ISO 8601 strings. This ensures the log is stable across Python
versions and can be read by external tools.

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
