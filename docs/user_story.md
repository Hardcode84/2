# User Story: Multi-Project Agent Workflow

## Design principle

LLMs cannot reliably execute multi-step tool choreography. Every chained
tool call is a chance to drop a step. Therefore:

- **The system (CLI/scripts) handles all structural work**: creating agents,
  workspaces, linking directories, wiring the tree.
- **Agents handle cognitive work**: reading code, writing code, reviewing,
  summarizing. One-call delegation at most.
- **Workspace inheritance** makes delegation trivial: `spawn_agent` without
  explicit workspace gives the child the parent's workspace. One tool call
  to delegate, zero plumbing.

## Agents

* **Root agent** — monitor and summarizer. Lightweight workspace for scratch
  notes. Does NOT create project agents — they are created by the CLI.
  Tracks project status, answers user questions about overall progress.
* **Project agent** (N, one per project) — owns the repo in its workspace.
  Created by CLI with workspace pre-linked. Receives tasks directly from the
  user. Can spawn workers (one call, workspace inherited). Manages feature
  lifecycle.
* **Worker agent** (per feature) — inherits project agent's workspace or gets
  an isolated worktree. Does actual coding work. Spawned by project agent.
* **Reviewer agent** (persistent, per project) — sibling of workers under
  project agent. Reads worker workspaces via ro views.

## Feature lifecycle

1. User sends task directly to project agent:
   `substrat agent send wave "implement feature X"`.
2. Project agent spawns a worker (one tool call, workspace inherited):
   `spawn_agent("worker-X", instructions="<task details>")`.
3. Worker does the work, messages parent when done.
4. Project agent reviews or spawns a reviewer.
5. When satisfied, project agent integrates (merge branch if applicable).
6. Project agent messages root with a summary for the user.
7. Worker terminated. Reviewer persists.

For complex features, project agent creates a git worktree first (shell
command in its own workspace), then spawns the worker with an isolated
workspace. But for simple tasks, workspace inheritance is sufficient.

## Workspace topology

```
root (lightweight ws, scratch notes)
├── project-A (ws: repo A, owns worktrees)
│   ├── worker-1 (inherits ws, or isolated worktree)
│   ├── worker-2 (inherits ws, or isolated worktree)
│   └── reviewer (ws: ro view of worker worktree)
└── project-B (ws: repo B, owns worktrees)
    ├── worker-3 (inherits ws)
    └── reviewer (ws: ro view)
```

## Key design points

* **External skeleton.** The CLI creates root, project agents, and their
  workspaces. Agents never create other agents at the same level — project
  agents are peers set up by the human. Agents CAN spawn children (workers,
  reviewers) but the one-call workspace inheritance makes this trivial.
* **Direct task submission.** Users send tasks to project agents directly,
  not through root. Root is a monitoring/summary layer, not a dispatcher.
  This eliminates the root-as-relay failure mode.
* **Workspace inheritance.** `spawn_agent` without `workspace=` gives the
  child the parent's workspace. The 4-step workspace dance
  (create + link + link + spawn) becomes one call. This is the key enabler.
* **Worktree isolation (optional).** For features that need branch isolation,
  the project agent creates a worktree in its own workspace (one shell
  command), then uses `create_workspace` + `link_dir` for the worker. But
  this is opt-in for complex features, not the default path.
* **Beads as shared state (optional).** When `br` is available, agents use
  the dependency graph for task sequencing instead of message-based
  coordination. `br ready --claim` replaces "wait for parent to tell me
  what to do next."
* **One-hop routing.** Root can't reach workers directly (two hops). Project
  agent is the coordination layer. This is intentional.
* **Messages for escalation, beads for coordination.** `send_message` is
  for human-facing summaries and cross-layer escalation. Day-to-day task
  sequencing runs through the beads dependency graph (when available).

## Infrastructure required

### Workspace inheritance (new)

When `spawn_agent` is called without `workspace=`, the child automatically
gets the parent's workspace (same sandbox, same mounts). Implementation:
the orchestrator looks up the parent's workspace mapping and assigns it to
the child. No new tools needed — it's a default behavior change.

### Compound spawn (optional, future)

`spawn_worker(name, instructions, worktree=null)` — creates a worktree if
requested, sets up an isolated workspace with the worktree linked, and
spawns the worker. One tool call for the isolated-worktree case. Deferred
until simple inheritance proves insufficient.

## Deployment walkthrough

Concrete steps from cold start to checking results. Assumes the daemon
is installed and a repo (`~/code/project-A`) exists on the host.

### 1. Start the daemon

```bash
substrat daemon start --max-slots 4
```

### 2. Set up root and projects

```bash
# Create root coordinator (scratch workspace + agent).
./templates/scripts/init-root.sh root

# Create project agents with workspaces (links repo RW).
./templates/scripts/init-project.sh root project-A ~/code/project-A
./templates/scripts/init-project.sh root project-B ~/code/project-B
```

`init-project.sh` now creates the project agent (not just the workspace).
The agent's instructions are inlined — no file-reading indirection.

Verify:

```bash
substrat workspace list
substrat agent list
# root          [idle]
# root/project-A [idle]
# root/project-B [idle]
```

### 3. Submit a task

```bash
# Send directly to the project agent — skip root.
substrat agent send project-A "Check README for typos and fix them"
```

The project agent will:
1. Spawn a worker (one call, workspace inherited).
2. End its turn.
3. Worker does the work, messages parent.
4. Project agent relays result to root / user.

### 4. Monitor progress

```bash
# Tree view.
substrat agent list

# Inspect a specific agent.
substrat agent inspect project-A

# Event stream.
substrat daemon watch

# User inbox (messages from root or project agents).
substrat inbox
```

### 5. Iterate

```bash
substrat agent send project-A "Add input validation to signup endpoint"
substrat agent send project-B "Set up CI pipeline"
```

### 6. Tear down

```bash
substrat daemon stop
# Or nuclear: rm -rf ~/.substrat
```

## What changed from the original design

| Before | After |
|--------|-------|
| Root creates project agents | CLI creates project agents |
| User talks to root only | User talks to project agents directly |
| Project agent does 4-step workspace dance | Workspace inheritance (one call) |
| Templates read from /templates/*.md files | Instructions inlined at creation |
| Root is a dispatcher | Root is a monitor/summarizer |
| Multi-step tool choreography | Single-call delegation |
