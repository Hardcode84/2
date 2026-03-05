# User Story: Multi-Project Agent Workflow

## Agents

* **Root agent** — pure coordinator, no code. Lightweight workspace for scratch notes.
* **Project agent** (N, one per project) — owns a cloned repo in its workspace.
  Created via root: "start new project \<git repo\>".
* **Worker agent** (per feature) — own workspace with own clone, can wreck it freely.
  Spawned by project agent.
* **Reviewer agent** (persistent, per project) — sibling of workers under project agent.
  Reads worker workspaces via ro views.

## Feature lifecycle

1. User tells root: "I want feature X in project Y".
2. Root relays to the corresponding project agent.
3. Project agent spawns a worker for the feature.
   * Worker gets its own workspace with a fresh clone (network at startup only).
   * Worker writes code, commits to a feature branch.
4. Worker messages project agent when ready or has questions.
   * Project agent answers directly or relays to root/user.
5. Project agent creates ro view of worker's workspace, launches reviewer(s).
   * Reviewers read worker's code, message back and forth with worker.
   * Worker iterates until reviewers are satisfied.
6. Project agent pulls worker's branch into its own repo (ro view of worker workspace).
   Worker never touches project repo — project agent is the integration point.
7. Project agent relays to user for final review.
8. When user satisfied, worker is terminated. Reviewer persists for next feature.

## Workspace topology

```
root (lightweight ws, scratch notes)
├── project-A (ws: cloned repo A)
│   ├── worker-1 (ws: own clone of repo A)
│   ├── worker-2 (ws: own clone of repo A)
│   └── reviewer (ws: ro views of worker repos as needed)
└── project-B (ws: cloned repo B)
    ├── worker-3 (ws: own clone of repo B)
    └── reviewer (ws: ro view)
```

## Key design points

* **No cross-workspace writes.** Workers write only to their own workspace.
  Project agent reads worker workspace (ro view) and pulls into its own repo.
  No network needed for intra-project code movement.
* **Concurrent workers are safe.** Each has an isolated clone. Project agent
  is the single integration point — pulls sequentially, resolves conflicts
  in its own repo.
* **One-hop routing.** Root can't reach workers directly (two hops). Project
  agent is the coordination layer. This is intentional.
* **User communication.** Root sends results to the user via
  `send_message("USER", ...)`. The CLI reads them with `substrat inbox`.

## Deployment walkthrough

Concrete steps from cold start to checking results. Assumes the daemon
binary is installed and two repos (`~/code/project-A`, `~/code/project-B`)
exist on the host.

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

Save the returned agent ID:

```bash
ROOT=<agent-id-hex>
```

### 4. Submit a task

```bash
substrat agent send $ROOT "Implement a REST endpoint for user signup in project-A"
```

Or interactively:

```bash
substrat agent attach $ROOT
> Implement a REST endpoint for user signup in project-A
```

The root agent will:
1. See that project-A has no agent yet.
2. Discover `project-A-ws` via `list_workspaces`.
3. `spawn_agent("project-A", instructions=<project template>, workspace="project-A-ws")`.
4. `send_message("project-A", "implement REST endpoint for user signup")`.

The project agent will then spawn a worker, the worker writes code, a
reviewer is spawned, and the cycle runs autonomously.

### 5. Monitor progress

```bash
# Tree view — see all agents and their states.
substrat agent list

# Inspect a specific agent.
substrat agent inspect $ROOT

# Tail all event logs in real time.
substrat daemon watch

# Tail one agent's events.
substrat daemon watch --agent-id $ROOT
```

### 6. Check results

Messages from the root agent to the user land in the inbox:

```bash
substrat inbox
# 14:32:07  from=root  feature X integrated in project-A, branch merged to main
```

The inbox drains on read — each call returns new messages since the last
check. For continuous monitoring, poll in a loop or combine with
`daemon watch`.

### 7. Iterate

Send follow-up tasks to the same root agent. It reuses existing project
agents and their persistent reviewers:

```bash
substrat agent send $ROOT "Add input validation to the signup endpoint in project-A"
substrat agent send $ROOT "Set up CI pipeline in project-B"
```

### 8. Tear down

```bash
# Terminate a specific agent (must be a leaf — terminate children first).
substrat agent terminate <worker-id>

# Stop the daemon (terminates all agents).
substrat daemon stop
```

### Expected agent tree after step 4

```
substrat agent list
<root-id>       root        [idle]
<proj-A-id>     project-A   [busy]    parent=<root-id>
<worker-id>     worker-signup [busy]  parent=<proj-A-id>
```

After the worker finishes and a reviewer is spawned:

```
<root-id>       root          [idle]
<proj-A-id>     project-A     [idle]  parent=<root-id>
<worker-id>     worker-signup  [idle]  parent=<proj-A-id>
<reviewer-id>   reviewer       [busy]  parent=<proj-A-id>
```
