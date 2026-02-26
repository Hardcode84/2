# Substrat

"Субстрат"

Agent orchestration framework.

## Architecture

* Daemon + CLI split: long-running daemon owns all state, thin CLI client for user interaction.
* Pluggable provider abstraction — each LLM/agent backend implements a common protocol (factory creates sessions, sessions handle send/suspend/stop). Providers are interchangeable.
* Agent capabilities (spawning, messaging, file access) are exposed as tool calls. The delivery mechanism is provider-specific.

## Layers

### Agent sessions

Sessions are the lowest layer — the substrate that the agent hierarchy and messaging are built on top of. They deal only with provider lifecycle and context persistence; they know nothing about trees or messages.

* Multiple supported LLM/agent providers/models.
* Each agent has exactly one session. Session is a long-lived object, named with UUID. One session = one agent = one provider instance.
* Session encapsulates the agent's context management (native session ID for agentic providers, actual conversation context for bare LLM providers).
* Sessions can be suspended/restored/deleted independently.
* Active sessions multiplexed across a limited number of concurrently running provider instances.
* Log EVERYTHING — structured log for full replayability, plaintext transcript for human inspection.

### Agent hierarchy

* Agents can launch new subagents in teams, these can launch their subagents too, unlimited depth.
* Each subagent can be given a name and custom instructions.
* Strict one-hop routing: agents can communicate horizontally within team (siblings), up/down one level only (parent/children). No skipping levels.
* Synchronous messages (agent is blocked until reply is received), asynchronous inbox/outbox, multicast (ask multiple opinions in team).
* Upper agent can inspect what subordinates are doing.
* Few "root" agents (maybe 1), communicate with user through CLI.
* Agents self-organize into "Managers", "Workers". Manager can run 1 agent to write the code, multiple agents for review, etc. Roles are advisory labels, not enforced by routing.

### Workspaces

* Work is organized in isolated workspaces, managed independently from the agents.
* Sandboxed isolation, optional access to network.
* Agents can use native provider tools in the workspace.
* Symlinks (RO, RW) into external dirs (e.g. project repo).
* Multiple agents can use same workspace (potentially with different permissions).
* Hierarchical workspaces — agents can mark a subfolder in their workspace as a workspace for a launched team.
* Symlinks within the workspace — link something from your workspace to the child agent workspace.
* Agents maintain long-term context via files/todo in workspace (other tools TBD).
