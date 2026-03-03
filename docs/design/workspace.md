# Workspace Model

Workspaces are independent named resources that provide sandboxed filesystem
environments. They know nothing about agents, sessions, or the hierarchy — they
are pure infrastructure. The daemon bridges workspaces and agents at runtime.

Workspace access is scoped: each workspace has a creator scope (an agent UUID),
and visibility follows the agent tree — own scope, children's scopes, parent's
scope. The scoping mechanism is enforced by the daemon, not the workspace model.

## Data Model

```python
@dataclass
class Workspace:
    name: str                              # Local name (unique within scope).
    scope: UUID                            # Creator agent ID. Frozen at creation.
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

The unique key is `(scope, name)` — two agents can independently create
workspaces named `"output"` without collision.

Links are bind mounts, not filesystem symlinks. They map an external host
directory into the workspace's virtual filesystem at `mount_path`. Mode
controls whether the bind is read-only or read-write.

Note: the data model uses `host_path`/`mount_path` internally. Agent-facing
tool JSON uses `source`/`target` for readability — the daemon translates.

## Filesystem Layout

```
~/.substrat/workspaces/<scope-hex>/<name>/
├── meta.json       # Workspace spec snapshot.
├── events.jsonl    # Workspace operations log.
└── root/           # Backing directory — workspace content lives here.
```

Workspaces are grouped by scope (creator agent UUID). CLI-created workspaces
use the `USER` sentinel (`00000000-0000-0000-0000-000000000001`) as scope.

## Scoping and Name Resolution

Agents reference workspaces by scoped name. The daemon resolves references
by walking the agent tree from the caller, same as message routing.

### Reference syntax

| Reference          | Resolves to                                    |
|--------------------|------------------------------------------------|
| `"my-ws"`          | Workspace in the calling agent's own scope.    |
| `"worker/output"`  | Workspace in child `worker`'s scope.           |
| `"../shared"`      | Workspace in parent's scope.                   |

No deeper references (`../../grandparent/ws`) — one hop only, mirroring the
messaging model. If you can't name it, you can't see it.

### Visibility and access

| Scope              | Visible | Mutable |
|--------------------|---------|---------|
| Own                | yes     | yes     |
| Children's         | yes     | yes     |
| Parent's           | yes     | no      |

Parent supervises children — full access to children's workspaces (create,
link, unlink, delete). Children can see parent's workspaces (for `view_of`
references) but cannot modify them. Sibling branches are invisible.

Root agents see CLI-created (USER-scoped) workspaces as parent scope:
visible, read-only.

### Resolution algorithm

```python
def resolve(caller: AgentNode, ref: str, tree: AgentTree) -> tuple[UUID, str]:
    """Resolve a workspace reference to (scope, local_name)."""
    if "/" not in ref and not ref.startswith(".."):
        return (caller.id, ref)                    # Own scope.
    parts = ref.split("/")
    if parts[0] == "..":
        parent = tree.parent(caller.id)
        scope = parent.id if parent else USER      # Root's parent is USER.
        return (scope, parts[1])
    child = tree.child_by_name(caller.id, parts[0])
    return (child.id, parts[1])
```

## Operations

Workspace operations are pure data model manipulations. They do not touch
bwrap or running processes — the sandbox is rebuilt from the current spec
on each agent turn.

### Create

Create a workspace in the caller's scope. Allocates the backing directory and
writes initial metadata. Name must be unique within the scope.

### Delete

Delete a workspace, its backing directory, and all workspaces that are live
views of it (recursively). The entire view tree is deleted as a unit. Fails
if any workspace in the tree has alive (non-terminated) agents assigned to it.
The daemon enforces this — checks the agent-workspace mapping for every
workspace in the view tree before allowing deletion.

**v1 limitation:** View tree cascade is deferred. `delete_workspace` checks
direct agent assignments only — it does not discover or delete dependent views.
Deleting a source workspace while views exist leaves orphaned links. This is a
known gap, not a bug.

View tree discovery: a workspace B is a view of workspace A if any of B's
links point into A's `root_path`. The daemon maintains this dependency graph
(derived from `LinkSpec.host_path` at link time). Deleting a leaf view does
not affect the source workspace.

### Link / Unlink

Add or remove a bind mount entry. Takes effect on the next bwrap invocation
(next agent turn). Unlinking does not delete the source directory.

### Create Live View

Convenience operation: creates a new workspace (in the caller's scope) with
a single link to another workspace's directory. Not a distinct data model
concept — it produces a regular `Workspace` whose sole link points at a
(sub)directory of the source workspace's backing dir.

```python
def create_view(
    source_ref: str, name: str, subdir: str = ".", mode: str = "ro",
) -> Workspace:
    source_ws = resolve_and_get(source_ref)  # Visibility check.
    host_path = source_ws.root_path / subdir
    ws = create(name)                        # In caller's scope.
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

An agent can create a view of a parent-scoped workspace — `view_of` only
requires visibility, not mutability. This is the typical pattern: parent's
workspace is read-only to the child, but the child can create a view of it
in its own scope and assign that view to a grandchild.

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

### System dependencies

The default system read-only binds (`/usr`, `/bin`, `/lib`, `/lib64`, `/sbin`,
`/etc`) cover libraries and config. On systemd-resolved hosts, DNS resolution
also requires `/run` (read-only) — the stub resolver at `127.0.0.53` talks to
`systemd-resolved` via a socket under `/run/systemd/resolve/`. Without it,
`getaddrinfo` fails for any provider that needs network access.

## Provider Bind Requirements

Providers (cursor-agent, Claude CLI, etc.) need access to host directories for
their own session management. These requirements are per-provider-type, not
per-session — the provider stores its own sessions in subdirectories keyed by
workspace hash or session UUID.

The daemon passes a `wrap_command` callback to subprocess-based providers.
The provider passes its required bind mounts and env vars to the callback
at each subprocess invocation. The daemon's closure merges these with
workspace config inside `build_command`. See [provider.md](provider.md)
for the callback signature.

Typical provider bind mounts:

| Provider       | Host path              | Mount path             | Mode |
|----------------|------------------------|------------------------|------|
| cursor-agent   | `~/.local`             | `~/.local`             | ro   |
| cursor-agent   | `~/.cursor`            | `~/.cursor`            | rw   |
| cursor-agent   | `~/.config/cursor`     | `~/.config/cursor`     | ro   |
| claude-cli     | `~/.claude/`           | `~/.claude/`           | rw   |

See `docs/design/providers/cursor_agent.md` for the full breakdown of why
cursor-agent needs these paths.

## Agent-Workspace Mapping

The daemon maintains `dict[UUID, tuple[UUID, str]]` — agent ID to `(scope,
name)`. This is daemon-level state, not part of either model. Constraints
enforced by the daemon:

- An agent has at most one workspace.
- A workspace can have multiple agents.
- Cannot delete a workspace while any assigned agent is alive (not terminated).

On agent termination, the daemon removes the mapping entry. On daemon crash
recovery, the mapping is reconstructed from agent records (each `AgentNode`
stores its workspace scope and name).

## Agent-Facing Tools

**Status:** All five workspace tools are implemented in `src/substrat/agent/tools.py`
as methods on `ToolHandler`. The tool catalog (`WORKSPACE_TOOLS`) and unified
`ALL_TOOLS` tuple are exported from `substrat.agent`. Daemon and MCP server
use `ALL_TOOLS` for dispatch.

Agents manage workspaces through MCP tools. All tools are non-blocking and
return immediately. Workspace references use the scoped syntax described in
[Name Resolution](#reference-syntax).

### `create_workspace`

Creates a workspace in the calling agent's scope.

```
Parameters:
  name: str
  network_access: bool = false
  view_of: str | null = null       # Source workspace ref for live view.
  subdir: str = "."                # Subfolder within source (view_of only).
  mode: "ro" | "rw" = "ro"        # View mode (view_of only).

Returns:
  {"status": "created", "name": "str"}
```

When `view_of` is set, creates a live view. The `view_of` reference follows
scoped resolution — an agent can view its own or its parent's workspaces:

```json
{"name": "child-view", "view_of": "../project-ws", "subdir": "src", "mode": "ro"}
```

### `link_dir`

Link a directory into a workspace. The `source` path is resolved relative to
the calling agent's workspace — the daemon translates virtual paths to host
paths using the agent's workspace spec. The calling agent must have a workspace
assigned; the call fails otherwise.

The target workspace must be in a mutable scope (own or children's).

```
Parameters:
  workspace: str           # Target workspace ref (scoped).
  source: str              # Path inside the agent's own workspace.
  target: str              # Mount path inside target workspace.
  mode: "ro" | "rw" = "ro"

Returns:
  {"status": "linked"}
```

The daemon validates that the resolved host path exists at link time. If it
does not, the call fails immediately rather than producing a broken bwrap
invocation later.

For linking host paths directly (e.g. a project repo), use the CLI.

### `unlink_dir`

Target workspace must be in a mutable scope (own or children's).

```
Parameters:
  workspace: str           # Workspace ref (scoped).
  target: str              # Mount path to remove.

Returns:
  {"status": "unlinked"}
```

### `delete_workspace`

Target workspace must be in a mutable scope (own or children's).

```
Parameters:
  name: str                # Workspace ref (scoped).

Returns:
  {"status": "deleted"}
```

Fails if any agent is assigned to the workspace (or any workspace in its
view tree — see [Delete](#delete)).

### `list_workspaces`

List visible workspaces (own scope + children's scopes + parent's scope).

```
Parameters: (none)

Returns:
  {"workspaces": [{"name": "str", "scope": "self" | "parent" | "<child-name>", "mutable": bool}, ...]}
```

### `spawn_agent` integration

`spawn_agent` accepts a workspace reference. The workspace must already exist
and be visible to the caller.

```
Parameters:
  name: str
  instructions: str
  workspace: str | null    # Workspace ref (scoped).

Returns:
  {"status": "accepted", "agent_id": "uuid", "name": "str", "workspace": "str" | null}
```

For convenience, `spawn_agent` also accepts an inline workspace spec. The
daemon decomposes it into individual workspace operations before processing
the spawn — on the data model level, these are separate operations. If the
deferred spawn fails after the workspace was created, the daemon deletes the
workspace (cleanup, not rollback — the workspace had no agent assigned yet).

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

The inline workspace is created in the caller's scope (the parent), not the
child's scope. The parent owns it and can modify it later.

### Typical flow

1. Manager creates workspace: `create_workspace("worker-env")`.
2. Manager links source code: `link_dir("worker-env", source="/src", target="/src", mode="ro")`.
3. Manager spawns worker: `spawn_agent("worker", instructions="...", workspace="worker-env")`.
4. Daemon assigns worker to `worker-env` (scope = manager), builds bwrap, launches.

Worker wants to create a sub-workspace for a reviewer:

1. Worker creates view: `create_workspace("review-view", view_of="../worker-env", subdir="/src", mode="ro")`.
2. Worker spawns reviewer: `spawn_agent("reviewer", instructions="...", workspace="review-view")`.
3. Reviewer sees `/src` read-only. Reviewer cannot see manager's workspaces (two hops away).

## CLI Commands

The CLI operates outside the agent tree. It uses the `USER` scope for creating
workspaces, and fully qualified `<scope>/<name>` syntax for referencing
workspaces in other scopes. Host paths are used directly — no virtual path
resolution.

```
substrat workspace create <name> [--network]
substrat workspace delete [<scope>/]<name>
substrat workspace list [--all | --scope <agent>]
substrat workspace link [<scope>/]<name> --source <host-path> --target <mount-path> [--mode rw]
substrat workspace unlink [<scope>/]<name> --target <mount-path>
substrat workspace view <source-ref> --name <view-name> [--subdir <path>] [--mode rw]
substrat workspace inspect [<scope>/]<name>
```

Root agent creation takes a workspace name (USER-scoped):

```
substrat workspace create my-project
substrat workspace link my-project --source ~/code/project --target /project --mode rw
substrat agent create --provider cursor-agent --workspace my-project
```

## Logging

Workspaces have their own append-only event log at
`~/.substrat/workspaces/<scope-hex>/<name>/events.jsonl`. Logs workspace-level
operations: creation, link/unlink, RO view creation, deletion. Does not log
agent activity within the workspace — that belongs to the agent's own event
log.

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
- **Workspace-level permissions.** Currently permissions are per-link. Should
  there be a workspace-wide default mode?
- **Scope transfer.** Can workspace ownership be transferred to another agent?
  Currently scope is frozen at creation. May be useful for long-lived
  workspaces that outlive their creators.
