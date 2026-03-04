# TODO

## Research
- [x] Investigate how to add custom tools to cursor CLI — MCP via .cursor/mcp.json
- [x] Investigate cursor-agent session storage — local SQLite, not server-side
- [x] Investigate how to make cursor CLI work inside bwrap — needs network, /run for DNS, ~/.local (ro), ~/.cursor (rw), ~/.config/cursor (ro)
- [x] Verify .cursor/rules/*.mdc loading in headless CLI mode inside bwrap — works, e2e test in test_cursor_rules_bwrap.py

## Session Layer
- [x] Provider protocol (`provider/base.py`) — includes models() for model discovery, model param is str | None (provider chooses default)
- [x] Session model (`session/model.py`)
- [x] Session store — atomic_write persistence, startup recovery (ACTIVE → SUSPENDED)
- [x] Session multiplexer — LRU slot management, acquire/release
- [x] Turn scheduler — turn execution (acquire → send → release), deferred work, session lifecycle

## Agent Layer
- [x] Agent node + tree — AgentNode dataclass, AgentTree queries (children, parent, team, roots)
- [x] Messaging — MessageEnvelope, routing validation (one-hop), inbox
- [x] Tool handler — agent-facing tool surface (spawn_agent, send_message, broadcast, check_inbox, inspect_agent, complete)
- [x] MCP server — Substrat tool surface for agents (daemon_dispatch implemented)

## Orchestrator
- [x] Orchestrator — composition root bridging agent and session layers
- [x] Deferred spawn — child sessions created after parent's turn ends
- [x] Provider/model inheritance via spawn callback closure
- [x] Crash recovery design — agent tree derived from event logs (docs/design/crash_recovery.md)
- [x] Crash recovery implementation — tree persistence via event logs, daemon startup replay

## Workspace
- [x] Workspace design doc (docs/design/workspace.md)
- [x] Workspace data model implementation — Workspace, LinkSpec, WorkspaceStore
- [x] Scoped name resolution — resolve agent-relative refs to (scope, name)
- [x] Agent-workspace mapping — bidirectional index, assign/release/lookup
- [x] bwrap command builder — build_command + check_available sandbox probe
- [x] Workspace MCP tools — create_workspace, link_dir, unlink_dir, delete_workspace, list_workspaces
- [x] View tree tracking — BFS discovery in store.view_tree(), cascade delete in delete_workspace
- [x] CLI workspace commands — create, delete, list, link, unlink, view, inspect

## Daemon
- [x] UDS server + event loop
- [x] Session + agent registries
- [x] Request dispatch (CLI ↔ daemon)
- [x] daemon_dispatch() in mcp_server.py — UDS client that sends tool calls to daemon
- [ ] .mdc rules bind-mount wiring — daemon generates per-agent rules, binds into workspace at bwrap time

## Wake Failure Handling
Design: [docs/design/wake.md — Wake Failure Handling](docs/design/wake.md)
- [x] Bug: `_process_wake` has no try/except around `_execute_turn` — one child crash kills the wake loop for all agents
- [x] Peek-then-drain: `_format_wake_prompt` uses `peek()`, drain via `collect()` only after turn succeeds — messages preserved on failure
- [x] Parent error notification: deliver `MessageKind.ERROR` to parent inbox on wake-turn failure, include exception + consumed message summaries
- [x] `poke` tool: re-wake child without sending a message — retries failed turn with original inbox contents
- [x] MCP catalog entry for `poke`

## Daemon — Bugs
- [x] `tool.call` allows calling arbitrary ToolHandler methods via getattr — whitelisted against ALL_TOOLS names (6de69e8)
- [x] `spawn_agent` param mismatch — MCP catalog and method now agree on `workspace` param (6de69e8)
- [x] Malformed JSON from client — now returns ERR_INVALID error envelope (6de69e8)
- [x] `PermissionError` in `_cleanup_stale` — PermissionError treated as "process alive" (6de69e8)
- [x] `AgentStateError` not caught — now mapped to ERR_INVALID (09a0c0e)
- [x] `TurnScheduler._deferred` dead code — removed (09a0c0e)
- [x] Wrong type annotation in test fixture — fixed to collections.abc.AsyncGenerator (09a0c0e)

## CLI
- [x] Typer app skeleton
- [x] daemon start/stop/status
- [x] agent create/list/send/inspect/terminate
- [ ] agent attach (bidirectional streaming)
- [ ] session list/suspend/resume/delete
- [x] workspace create/delete/list/link/unlink/view/inspect

## EventLog
- [x] No directory fsync after file creation — new file's dir entry not durable on crash
- [x] No context manager (__enter__/__exit__) — fd leaks if stop() never called in non-crash path
- [x] `str(self._path)` wrapping unnecessary — os.open accepts PathLike since 3.6

## @log_method Decorator
- [x] Generator path hardcodes str join — "".join(chunks) crashes on non-string yields, masks exception in finally
- [x] Inconsistent after-log shape: generator logs {"text":...}, coroutine logs {**args, "result":...}
- [x] _serialize_result incomplete — only handles bytes, needs str() fallback for Path/UUID/etc
- [x] No Loggable protocol to enforce self._log attribute at type-check time
- [x] Return type Callable[[Any], Any] too loose — TypeVar _F preserves decorated signatures

## Provider / Logging Integration
- [x] workspace=Path("/tmp") hardcoded in CursorAgentProvider.create() — now uses tempfile.mkdtemp(), private_workspace flag, cleanup on stop()
- [x] System prompt sent as first message — cursor-agent now writes .mdc rules instead, survives context compaction
- [x] System prompt persisted in suspend state — CursorSession.suspend() saves it, restore() re-writes .mdc (38a3270)
- [ ] Mixed logging patterns (direct log.log() vs @log_method) undocumented
- [ ] _build_args_dict doesn't enforce serialization contract on args — non-JSON-serializable arg will crash json.dumps
- [ ] base64 encoding for bytes not documented in serialization contract (session.md)
- [ ] transcript.txt companion log not implemented (referenced in implementation.md)

## Design Gaps
- [x] Stale provider blob — crash recovery design uses event log as source of truth, not provider_state blob
- [ ] Broadcast completion signal: agent has no way to know all replies arrived
- [ ] Sync message timeout: recipient crash leaves sender permanently stuck — partially addressed by wake failure → parent notification, but no timeout mechanism
- [x] spawn_agent can't specify provider/model — children inherit parent's provider/model via closure

## Agent Runtime
- [ ] Child system prompt reinjection — parent tool to update child's instructions without restarting it; applied on next child turn; bare LLM providers edit history/context directly, agentic providers (cursor-agent) update the .mdc rules file and reapply on next context compaction (or force compaction)

## Messaging — Deferred
- [x] Self-send gives confusing "cannot reach" error — validate_route rejects it implicitly, needs explicit guard or clear message
- [ ] Agents cannot route messages to SYSTEM/USER recipients — sentinels not in tree, daemon boundary layer needs to handle this
- [x] Inbox.collect() non-atomic — add comment documenting single-threaded assumption (list + clear is two steps)
- [ ] MessageEnvelope.recipient=None not validated for non-broadcast kinds — REQUEST/RESPONSE should require a recipient
- [x] Mutable envelopes shared on broadcast — broadcast() creates fresh envelope per sibling, not shared
- [ ] Root-to-root routing impossible — multiple roots can't communicate, intentional but needs mechanism if multi-root becomes real

## Stale Docs
- [x] tool_integration.md still describes daemon_dispatch as a stub — updated with full workspace tool catalog (e43d312)
- [ ] implementation.md lists agent attach, session commands, workspace commands as if they exist — they don't yet

## Tests — Existing
- [x] test_provider_base.py
- [x] test_session_model.py
- [x] test_cursor_agent.py (unit + integration e2e)
- [x] test_event_log.py
- [x] test_log_decorator.py
- [x] test_orchestrator.py — unit tests covering create, run_turn, spawn, terminate, get_handler, wake loop, complete lifecycle
- [x] test_orchestrator_fuzz.py — Hypothesis stateful fuzzer (stress, --run-stress) — see fuzzer gaps below
- [x] test_recovery.py — crash recovery unit tests (log reading, tree reconstruction, recovery wake)
- [x] test_tools.py — tool handler unit tests (routing, messaging, spawn, workspace tools, wake callback, complete, inbox filtering)

## Tests — Missing Coverage
- [ ] EventLog: double open (leaks first fd), double close, empty pending file, large entry (>page size), log after close
- [ ] Decorator: keyword-only args, default values omitted, empty generator (yields nothing), before=True/after=False, before=False/after=False
- [ ] Daemon: malformed JSON over UDS, empty request body, missing required fields, invalid UUID strings
- [x] Daemon: concurrent tool.call during run_turn — ScriptedProvider + test_tool_callbacks.py (a2998ea)
- [ ] Daemon: _cleanup_stale branch with no PID file + orphaned socket
- [ ] Daemon: handler raising unexpected Exception → ERR_INTERNAL mapping
- [ ] CLI: daemon stop with a live process (actual SIGTERM + socket disappear flow)
- [ ] CLI: daemon start socket wait timeout warning path
- [ ] CLI: daemon status with stale PID, status with live PID but missing socket
- [ ] CLI: weak assertions — agent list parent display, Popen args in daemon start, inspect inbox formatting
- [x] FakeProvider hides streaming latency — ScriptedProvider in tests/helpers.py yields control mid-turn (a2998ea)
- [x] Session store, multiplexer, agent tree, messaging
- [x] test_session_store.py
- [x] test_persistence.py
- [x] test_multiplexer.py
- [x] test_scheduler.py
- [x] test_agent_tree.py
- [x] test_messaging.py
- [x] test_mcp_server.py — MCP stdio server unit tests
- [x] test_bwrap.py — bwrap sandbox builder tests
- [x] test_workspace_mapping.py — agent-workspace bidirectional mapping
- [x] test_workspace_model.py — workspace data model
- [x] test_workspace_resolve.py — scoped name resolution
- [x] test_workspace_store.py — workspace persistence (includes view_tree tests)
- [x] Crash-recovery fuzzer — test_crash_fuzz.py (single orch) + test_orch_crash_fuzz.py (dual orch)
- [x] test_rpc.py — wire protocol unit tests (sync_call, async_call, error envelopes, daemon_dispatch)
- [x] test_daemon.py — daemon handler dispatch, lifecycle, workspace tool dispatch, wake loop lifecycle
- [x] test_cli.py — CLI commands with mocked RPC
- [x] test_daemon_rpc.py — integration tests: full lifecycle, tool.call, concurrency, recovery over real UDS
- [x] test_tool_callbacks.py — integration tests: tool callbacks mid-turn over real UDS (spawn, message, complete, inspect)

## Orchestrator Fuzzer Gaps
The stateful fuzzer (`test_orchestrator_fuzz.py`) covers lifecycle interleavings but has blind spots around failure injection and agent coordination under stress.
- [x] Shadow state bug: children of flaky agents inherit flaky provider but aren't in `flaky_agents` — `run_turn` picks one, unhandled RuntimeError crashes the fuzzer
- [x] ChaosProvider: Hypothesis-controlled failure schedule (deque of outcomes: False/True/int), replaces binary FlakyProvider — covers create/send/suspend/restore failures with partial-send support
- [ ] Wake loop: fuzzer doesn't call `start_wake_loop` — message delivery → wake → turn → fail → re-wake path is completely untested under random interleaving
- [x] `complete()` rule: child calls complete (message parent + self-terminate) — interleaving with parent turns, sibling messages, and provider failures
- [x] Eviction failure paths: ChaosProvider suspend() can fail during eviction — exercises multiplexer rollback under random interleaving
- [x] Spawn failure interleaving: ChaosProvider create() failure during deferred drain — shadow model reconciles orphaned children against real tree
- [ ] Multiplexer invariants: no check that evicted sessions land in SUSPENDED state in the store or that restore round-trips correctly after eviction

### ChaosProvider Design (implemented)
See `tests/stress/test_orchestrator_fuzz.py`. ChaosProvider + ChaosProviderSession with Hypothesis-controlled deque schedule (False=succeed, True=fail, int=partial send). Per-provider shared schedule, drawn upfront via `@initialize`. Shadow model reconciles via `_shadow_drain` — checks which pending children survived deferred create().

## E2E — Blockers
Code bugs that prevent the full stack from working end-to-end.
- [x] `--approve-mcps` missing from CursorSession._build_cmd() — added when tools configured
- [x] `workspace=Path("/tmp")` fallback in CursorAgentProvider.create() — now uses tempfile.mkdtemp(), cleaned up on stop()
- [x] check_available() probe missed linker libs — /lib and /lib64 not bound, dynamically-linked /usr/bin/true failed inside sandbox (3b32c9f)
- [x] bwrap test fixtures used unresolved tilde mount paths — Path("~/.local") doesn't expand, cursor-agent invisible inside sandbox (3b32c9f)
- [x] CursorSession.send() swallowed stderr — non-zero exit with no output silently returned empty string (a2258e2)
- [ ] .mdc rules bind-mount wiring — only matters for shared workspaces (multiple agents same workspace); single-agent-per-workspace works fine with current host-side writes
- [x] Deferred spawn error recovery — do_spawn catches exceptions, removes orphaned child from tree/inbox/mapping

## E2E — Missing Integration Tests
- [x] Daemon + real cursor-agent (no bwrap) — daemon.start, agent.create, agent.send with CursorAgentProvider, verify real response
- [x] Daemon + workspace + bwrap + external MCP — workspace.create, agent.create(workspace=...), cursor-agent calls MCP tool inside bwrap sandbox (test_daemon_mcp_e2e.py)
- [ ] Daemon + bwrap + substrat MCP tools — agent inside bwrap discovers .cursor/mcp.json, MCP server connects back to daemon via SUBSTRAT_SOCKET, tool.call round-trip verified
- [ ] Multi-agent live coordination — parent spawns child via MCP tool, child sends message back, parent auto-wakes, verify full roundtrip (daemon-layer coordination proven in test_tool_callbacks.py; MCP→daemon chain still untested)
- [x] Session suspend/restore under daemon — daemon evicts session (LRU), next send restores from state blob, verify context survives

## Task Coordination
Tasks are files in shared workspaces, not a new abstraction. Completion is a message. Wakeup is inbox delivery. Taskwarrior available in sandbox — agents use it directly via CLI, no wrapper needed. Parent and child share a task dir via workspace links, coordinate through `task add`/`task done`. Prompt convention, not infrastructure.
- [x] `complete(result)` tool — sugar for "message parent + terminate self"
- [ ] `remind_me(reason, timeout, every=None)` tool — delayed self-message delivery; one-shot or repeating; cancelable
- [x] Auto-wake on inbox delivery — orchestrator fires agent turn when inbox gets a message (ca9176d..a996152)
- [x] `check_inbox` filtering — by sender, message kind

## Layer Isolation
- [x] `workspace/resolve.py` imports agent layer — `AgentNode`, `AgentTree`, `USER` from `substrat.agent`; should accept pre-computed scope sets instead (95521ea)
- [x] `agent/tools.py` ToolHandler holds workspace infrastructure — `ws_store`, `ws_mapping` and ~200 lines of workspace logic; extract to orchestrator callbacks (cebefa2, 8e9f85c)
- [x] `AgentNode.workspace` field — workspace assignment embedded in node, should live only in `WorkspaceMapping` (cebefa2)
- [x] Frozen `wrap_command` closure — factory now takes (scope, ws_name) and re-reads workspace from store each invocation

## Open Design Questions
- [ ] Configuration format — TOML? YAML? CLI flags only?
- [ ] Authentication model — daemon trusts any local socket connection, multi-user not addressed
- [ ] Resource limits — CPU/memory per workspace, token budgets per session
- [ ] Streaming UX — how agent attach handles interleaved output from multiple agents
- [ ] inspect_agent payload — "recent activity" undefined
- [ ] Tool reply injection mechanism — provider-specific?
- [ ] Tool rate limiting / abuse prevention
