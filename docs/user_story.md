# User Story: Multi-Project Agent Workflow

## Agents

* **Root agent** — pure coordinator, no code. Lightweight workspace for scratch notes.
* **Project agent** (N, one per project) — owns the repo in its workspace,
  creates git worktrees for workers. Created via root: "start new project \<git repo\>".
* **Worker agent** (per feature) — own workspace with a git worktree of the
  project repo. Isolated working tree, shared object store.
  Spawned by project agent.
* **Reviewer agent** (persistent, per project) — sibling of workers under project agent.
  Reads worker workspaces via ro views.

## Feature lifecycle

1. User tells root: "I want feature X in project Y".
2. Root relays to the corresponding project agent.
3. Project agent decomposes the request into a beads epic with child issues:
   * `br create --type=epic --title="feature X"` → epic ID.
   * `br create --title="implement API" --parent=<epic>` (repeat per subtask).
   * Dependencies between issues encode sequencing.
4. Project agent creates a git worktree and spawns a worker:
   * `git worktree add /worktrees/<feature> -b <feature>` in its repo.
   * Mounts the worktree into the worker's sandbox via `link_dir`.
5. Worker picks up work via `br ready --claim` and iterates:
   * Implement → commit → `br close <id> --suggest-next` → next issue.
   * If stuck, messages project agent via `send_message`.
6. When all implementation issues are closed, project agent spawns a
   reviewer with an RO view of the worktree.
   * Reviewer creates issues for feedback (linked to the epic).
   * Worker claims and fixes review issues the same way.
7. Project agent monitors progress via `br epic status`. When the epic
   is fully closed, merges the branch locally (`git merge <feature>`).
8. Project agent relays result to user via root.
9. Worker terminated. Reviewer persists for next feature.

## Workspace topology

```
root (lightweight ws, scratch notes)
├── project-A (ws: repo A, owns worktrees)
│   ├── worker-1 (ws: worktree of repo A, branch feature-1)
│   ├── worker-2 (ws: worktree of repo A, branch feature-2)
│   └── reviewer (ws: ro view of worker worktree)
└── project-B (ws: repo B, owns worktrees)
    ├── worker-3 (ws: worktree of repo B)
    └── reviewer (ws: ro view)
```

## Key design points

* **Worktree isolation.** Workers write only to their own worktree.
  The project agent merges branches locally — no cross-repo fetch needed
  because worktrees share the object store.
* **Concurrent workers are safe.** Each has an isolated worktree on its own
  branch. Git handles concurrent object writes atomically. Project agent
  is the single integration point — merges sequentially, resolves conflicts
  in its own working tree.
* **Beads as shared state.** `.beads/issues.jsonl` lives in the repo and is
  shared across worktrees. Each agent has its own SQLite DB, syncs via
  `br sync`. The dependency graph replaces most polling — agents pick up
  work as it becomes unblocked.
* **One-hop routing.** Root can't reach workers directly (two hops). Project
  agent is the coordination layer. This is intentional.
* **Messages for escalation, beads for coordination.** `send_message` is
  for human-facing summaries and cross-layer escalation. Day-to-day task
  sequencing runs through the beads dependency graph.
* **User communication.** Root sends results to the user via
  `send_message("USER", ...)`. The CLI reads them with `substrat inbox`.
  Users can also check `br epic status` directly for fine-grained progress.

## Deployment walkthrough

Concrete steps from cold start to checking results. Assumes the daemon
binary is installed, `br` is on PATH, and two repos (`~/code/project-A`,
`~/code/project-B`) exist on the host with beads initialized
(`br init` in each repo).

### 1. Start the daemon

```bash
substrat daemon start --max-slots 4
```

### 2. Create workspaces

One scratch workspace for the root coordinator, one per project with the
host repo linked in read-write.

```bash
# Root scratch pad.
substrat workspace create scratch

# Project A.
substrat workspace create project-A-ws
substrat workspace link project-A-ws $(substrat workspace list | grep project-A-ws | awk '{print $1}' | cut -d/ -f1) \
    --source ~/code/project-A --target /repo --mode rw

# Project B.
substrat workspace create project-B-ws
substrat workspace link project-B-ws $(substrat workspace list | grep project-B-ws | awk '{print $1}' | cut -d/ -f1) \
    --source ~/code/project-B --target /repo --mode rw
```

Verify:

```bash
substrat workspace list
# <scope>/scratch
# <scope>/project-A-ws  [net]  (if --network was passed)
# <scope>/project-B-ws
```

### 3. Seed the root agent

Pass the root coordinator template as instructions. The root agent's job
is pure dispatch — it never touches code.

```bash
substrat agent create root \
    --instructions "$(cat templates/root.md)" \
    --workspace scratch
```

### 4. Submit a task

```bash
substrat agent send root "Implement a REST endpoint for user signup in project-A"
```

Or interactively:

```bash
substrat agent attach root
> Implement a REST endpoint for user signup in project-A
```

The root agent will:
1. See that project-A has no agent yet.
2. Discover `project-A-ws` via `list_workspaces`.
3. `spawn_agent("project-A", instructions=<project template>, workspace="project-A-ws")`.
4. `send_message("project-A", "implement REST endpoint for user signup")`.

The project agent will then:
1. Decompose into issues:
   `br create --type=epic --title="user signup endpoint"` → epic.
   `br create --title="implement POST /signup" --parent=<epic>`.
   `br create --title="add input validation" --parent=<epic>`.
   `br create --title="write tests" --parent=<epic>`.
2. Create a worktree: `git worktree add /worktrees/signup -b signup`.
3. Create a worker workspace and mount the worktree into it.
4. Spawn a worker — it picks up issues via `br ready --claim`.
5. After implementation issues close, spawn a reviewer with an RO view.
   Reviewer creates review issues; worker fixes them the same way.
6. When `br epic status` shows the epic fully closed, merge locally:
   `git merge signup`.

### 5. Monitor progress

```bash
# Tree view — see all agents and their states.
substrat agent list

# Inspect a specific agent.
substrat agent inspect root

# Tail all event logs in real time.
substrat daemon watch

# Tail one agent's events.
substrat daemon watch --agent-id root

# Fine-grained task progress (run from project repo).
cd ~/code/project-A && br epic status
cd ~/code/project-A && br list --json
```

### 6. Check results

Messages from the root agent to the user land in the inbox:

```bash
substrat inbox
# 14:32:07  from=root  feature X integrated in project-A, branch merged to main
```

The inbox drains on read — each call returns new messages since the last
check. For continuous monitoring, poll in a loop or combine with
`daemon watch`. For task-level detail, check beads directly in the
project repo.

### 7. Iterate

Send follow-up tasks to the same root agent. It reuses existing project
agents and their persistent reviewers:

```bash
substrat agent send root "Add input validation to the signup endpoint in project-A"
substrat agent send root "Set up CI pipeline in project-B"
```

### 8. Tear down

```bash
# Terminate a specific agent (must be a leaf — terminate children first).
substrat agent terminate root/project-A/worker-signup

# Stop the daemon (terminates all agents).
substrat daemon stop
```

### Expected agent tree after step 4

```
substrat agent list
root                           [idle]
root/project-A                 [busy]
root/project-A/worker-signup   [busy]
```

After the worker finishes and a reviewer is spawned:

```
root                           [idle]
root/project-A                 [idle]
root/project-A/worker-signup   [idle]
root/project-A/reviewer        [busy]
```
