# Workspace Model

Workspaces are independent named resources that provide sandboxed filesystem
environments. They know nothing about agents, sessions, or the hierarchy — they
are pure infrastructure. The daemon bridges workspaces and agents at runtime.

## Data Model

```python
@dataclass
class Workspace:
    name: str                              # Unique identifier.
    root_path: Path                        # Host-side backing directory.
    network_access: bool = False
    links: list[LinkSpec] = field(default_factory=list)
    created_at: str = ""                   # ISO 8601.

@dataclass
class LinkSpec:
    host_path: Path                        # Source on host filesystem.
    mount_path: Path                       # Relative to workspace root.
    mode: Literal["ro", "rw"] = "ro"
```

Links are bind mounts, not filesystem symlinks. They map an external host
directory into the workspace's virtual filesystem at `mount_path`. Mode
controls whether the bind is read-only or read-write.

## Filesystem Layout

```
~/.substrat/workspaces/<name>/
├── meta.json       # Workspace spec snapshot.
├── events.jsonl    # Workspace operations log.
└── root/           # Backing directory — workspace content lives here.
```

## Operations

Workspace operations are pure data model manipulations. They do not touch
bwrap or running processes — the sandbox is rebuilt from the current spec
on each agent turn.

### Create

Create a named workspace. Allocates the backing directory and writes initial
metadata. Name must be unique across all workspaces.

### Delete

Delete a workspace and its backing directory. Fails if any agent is currently
assigned to the workspace. The daemon enforces this constraint — the workspace
model exposes a deletion hook, the daemon checks its agent-workspace mapping
before allowing it.

### Link / Unlink

Add or remove a bind mount entry. Takes effect on the next bwrap invocation
(next agent turn). Unlinking does not delete the source directory.

### Create Live View

Convenience operation: creates a new workspace with a single link to another
workspace's directory. Not a distinct data model concept — it produces a
regular `Workspace` whose sole link points at a (sub)directory of the source
workspace's backing dir.

```python
def create_view(
    source: str, name: str, subdir: str = ".", mode: str = "ro",
) -> Workspace:
    source_ws = store.get(source)
    host_path = source_ws.root_path / subdir
    ws = create(name)
    ws.links.append(LinkSpec(
        host_path=host_path,
        mount_path=Path("."),
        mode=mode,
    ))
    return ws
```

The view is live — changes to the source directory are visible immediately
(on the next bwrap invocation). Mode can be `"ro"` or `"rw"`, so the agent
can grant a child read-only or full access to a subtree of its workspace.

## Sandbox Execution

Each agent turn is a separate `bwrap` invocation. The daemon builds the bwrap
command from three sources, merged at launch time:

1. **Workspace spec** — `root_path` as the root bind, plus all `links`.
2. **Provider binds** — host directories the provider needs (session storage,
   config). See [Provider Bind Requirements](#provider-bind-requirements).
3. **MCP config** — read-only bind of the generated `.cursor/mcp.json` (or
   equivalent) from the daemon-managed location.

Because bwrap is rebuilt each turn, link/unlink operations made between turns
are picked up automatically. No hot-reloading needed.

## Provider Bind Requirements

Providers (cursor-agent, Claude CLI, etc.) need access to host directories for
their own session management. These requirements are per-provider-type, not
per-session — the provider stores its own sessions in subdirectories keyed by
workspace hash or session UUID.

```python
class AgentProvider(Protocol):
    name: str
    bind_mounts: Sequence[LinkSpec]        # Host dirs needed inside sandbox.
    async def create(self, ...) -> ProviderSession: ...
    async def restore(self, ...) -> ProviderSession: ...
```

`bind_mounts` is a class-level constant. The daemon queries all registered
providers at startup and caches the results. Examples:

| Provider       | Host path              | Mount path             | Mode |
|----------------|------------------------|------------------------|------|
| cursor-agent   | `~/.cursor/chats/`     | `~/.cursor/chats/`     | rw   |
| cursor-agent   | `~/.cursor/projects/`  | `~/.cursor/projects/`  | rw   |
| claude-cli     | `~/.claude/`           | `~/.claude/`           | rw   |

The provider manages its own subdirectory structure internally. Substrat mounts
the parent directory and stays out of the way.

## Agent-Workspace Mapping

The daemon maintains `dict[UUID, str]` — agent ID to workspace name. This is
daemon-level state, not part of either model. Constraints enforced by the
daemon:

- An agent has at most one workspace.
- A workspace can have multiple agents.
- Cannot delete a workspace while any assigned agent is alive (not terminated).

On agent termination, the daemon removes the mapping entry. On daemon crash
recovery, the mapping is reconstructed from agent records (each `AgentNode`
stores its workspace name).

## Agent-Facing Tools

Agents manage workspaces through MCP tools. All tools are non-blocking and
return immediately.

### `create_workspace`

```
Parameters:
  name: str
  network_access: bool = false
  view_of: str | null = null       # Source workspace name for live view.
  subdir: str = "."                # Subfolder within source (view_of only).
  mode: "ro" | "rw" = "ro"        # View mode (view_of only).

Returns:
  {"status": "created", "name": "str"}
```

When `view_of` is set, creates a live view of the source workspace (or a
subfolder of it). The agent can give a child RO access to its `src/` dir:

```json
{"name": "child-ws", "view_of": "parent-ws", "subdir": "src", "mode": "ro"}
```

### `link_dir`

Link a directory into a workspace. The `source` path is resolved relative to
the calling agent's workspace — the daemon translates virtual paths to host
paths using the agent's workspace spec.

```
Parameters:
  workspace: str           # Target workspace name.
  source: str              # Path inside the agent's own workspace.
  target: str              # Mount path inside target workspace.
  mode: "ro" | "rw" = "ro"

Returns:
  {"status": "linked"}
```

For linking host paths directly (e.g. a project repo), use the CLI.

### `unlink_dir`

```
Parameters:
  workspace: str
  target: str              # Mount path to remove.

Returns:
  {"status": "unlinked"}
```

### `delete_workspace`

```
Parameters:
  name: str

Returns:
  {"status": "deleted"}
```

Fails if any agent is assigned to the workspace.

### `spawn_agent` integration

`spawn_agent` accepts a workspace name. The workspace must already exist.

```
Parameters:
  name: str
  instructions: str
  workspace: str | null    # Name of pre-configured workspace.

Returns:
  {"status": "accepted", "agent_id": "uuid", "name": "str"}
```

For convenience, `spawn_agent` also accepts an inline workspace spec. The
daemon decomposes it into individual workspace operations before processing
the spawn — on the data model level, these are separate operations.

```
Parameters:
  name: str
  instructions: str
  workspace:
    name: str
    links: [{"source": "/path", "target": "/mount", "mode": "ro"}, ...]

Returns:
  {"status": "accepted", "agent_id": "uuid", "name": "str", "workspace": "str"}
```

### Typical flow

1. Parent creates workspace: `create_workspace("analysis-ws")`.
2. Parent links relevant dirs: `link_dir("analysis-ws", source="/data", target="/data", mode="ro")`.
3. Parent spawns child: `spawn_agent("analyst", instructions="...", workspace="analysis-ws")`.
4. Daemon assigns analyst to `analysis-ws`, builds bwrap with the workspace spec
   and provider binds, launches the provider process.

Or with the inline convenience form:

1. Parent spawns child with inline workspace:
   `spawn_agent("analyst", instructions="...", workspace={"name": "analysis-ws", "links": [{"source": "/data", "target": "/data", "mode": "ro"}]})`.

Both produce identical results.

## CLI Commands

Users configure workspaces for root agents via the CLI. Host paths are used
directly — no virtual path resolution.

```
substrat workspace create <name> [--network]
substrat workspace delete <name>
substrat workspace list
substrat workspace link <name> --source <host-path> --target <mount-path> [--mode rw]
substrat workspace unlink <name> --target <mount-path>
substrat workspace view <source-name> --name <view-name> [--subdir <path>] [--mode rw]
substrat workspace inspect <name>
```

Root agent creation takes a workspace name:

```
substrat agent create --provider cursor-agent --workspace my-project
```

## Logging

Workspaces have their own append-only event log at
`~/.substrat/workspaces/<name>/events.jsonl`. Logs workspace-level operations:
creation, link/unlink, RO view creation, deletion. Does not log agent activity
within the workspace — that belongs to the agent's own event log.

## Persistence

`meta.json` is written atomically (temp + fsync + rename) on creation and
after every link/unlink operation. Same pattern as session persistence.

On daemon startup, workspace metadata is loaded from disk. No state machine
recovery needed — workspaces are stateless resources. The agent-workspace
mapping is reconstructed from agent records.

## Open Questions

- **Workspace naming rules.** Alphanumeric + hyphens? Length limits? Reserved
  names?
- **Disk quotas.** Should workspaces have size limits?
- **Cleanup policy.** Auto-delete workspaces with no agents and no recent
  activity? Or manual-only for v1?
- **Cross-workspace links.** Can an agent link dirs from a workspace it is not
  assigned to? Current design says source is resolved from the agent's own
  workspace — linking from arbitrary workspaces would need a different
  mechanism.
- **Workspace-level permissions.** Currently permissions are per-link. Should
  there be a workspace-wide default mode?
