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
* **User communication.** Root-to-user delivery needs daemon boundary
  mechanism (currently a gap — agents can't route to USER sentinel).
