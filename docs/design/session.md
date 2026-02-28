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
    def __init__(self, store: SessionStore, max_slots: int = 4) -> None:
        self._slots: dict[UUID, ProviderSession] = {}
        self._lru: list[UUID] = []      # Released sessions, head = next victim.
        self._held: set[UUID] = set()   # Acquired, not evictable.

    async def put(self, session_id: UUID, ps: ProviderSession) -> None: ...
    async def acquire(self, session: Session, provider: AgentProvider) -> ProviderSession: ...
    async def release(self, session_id: UUID) -> None: ...
    async def remove(self, session_id: UUID) -> None: ...
```

`put()` slots a freshly-created provider session. `acquire()` returns a cached
slot or restores from suspension (evicting LRU if full). Sessions mid-send are
held (non-evictable); `release()` returns them to the LRU queue. `remove()`
tears down a session (calls `stop()`, no state blob saved). If all slots are
held when a new acquire arrives, `RuntimeError` — no async waiting for now.

### Slot interaction with tool calls

The two-phase messaging pattern (`send_message` with `sync=true`) and deferred
`spawn_agent` are both designed to minimize slot pressure:

- **Sync messaging**: agent sends, turn ends, slot released. Reply delivered as
  a new turn that re-acquires the slot. Between turns the session is evictable.
- **Deferred spawn**: `spawn_agent` returns immediately during the parent's
  turn. The daemon creates the child's provider session after the parent
  releases its slot, avoiding concurrent held slots for parent + child.

The multiplexer knows nothing about messaging or spawn queues — the daemon
layer above orchestrates `acquire()`/`release()`/`put()` calls at the right
times. See `tool_integration.md` for the full execution model.

## Turn Scheduler

`scheduler.py` ties sessions, providers, the multiplexer, and the store
together. Thin orchestration layer — no policy, just the acquire → send →
release lifecycle and deferred work drain.

```python
class TurnScheduler:
    def __init__(
        self,
        providers: dict[str, AgentProvider],
        mux: SessionMultiplexer,
        store: SessionStore,
        log_root: Path | None = None,
    ) -> None: ...

    async def create_session(self, provider_name: str, model: str, system_prompt: str) -> Session: ...
    async def send_turn(self, session_id: UUID, prompt: str) -> str: ...
    async def terminate_session(self, session_id: UUID) -> None: ...
    def defer(self, callback: DeferredCallback) -> None: ...
```

### In-memory session cache

The scheduler keeps `dict[UUID, Session]` in memory. The store is disk-only
with no cache — reading `session.json` every turn is wasteful. On crash, the
daemon recovers from disk via `store.recover()`.

If the multiplexer evicts a session behind the scheduler's back (LRU pressure
during another session's `put()` or `acquire()`), the cached copy goes stale.
`send_turn` detects this via `mux.contains()` and reloads from the store before
calling `acquire`.

### Turn lifecycle

1. Look up session and provider from in-memory caches.
2. Log `turn.start`.
3. `mux.acquire(session, provider)` — restores from suspension if needed.
4. Collect streamed chunks from `ps.send(prompt)`.
5. `mux.release(session_id)` — always runs (finally block).
6. Log `turn.complete`.
7. Drain deferred queue.

On error in step 4, the slot is released (finally) but steps 6–7 are skipped.
Failed turns should not trigger side effects like child spawns.

### Deferred work

`deque[Callable[[], Coroutine]]`. Synchronously filled during a turn (by MCP
handlers, `spawn_agent`, etc.), drained after slot release. Not `asyncio.Queue`
— no async await on enqueue, single consumer, simpler.

### Known gaps

- `mux.acquire` → `provider.restore` does not pass `EventLog`. The multiplexer
  is deliberately log-unaware. Provider-level `@log_method` events are lost for
  restored sessions. The scheduler's own `turn.start`/`turn.complete` events
  still work. Fix when the daemon layer owns log routing.
