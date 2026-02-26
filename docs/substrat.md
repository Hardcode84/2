# Substrat

"Субстрат"

Agents orchetration framework.

## Layers

### Agents sessions

* Multiple supported LLM/agents providers/models
* Agents organized in sessions
* Session is long living objest, names with UUID
* Encapsulates context management between providers (native session id for claude/cursor agents, actual context for bare LLM providers)
* Can be suspended/restored/deleted
* Sessions can be multiplexed between small number of active agents
* Log EVERYTHING, jsonl for full replayability + txt log for user inspection

### Agent hierarchy

* Agents can launch new subagents in teams, thise can lauch their subagent too, unlimited depth
* Each subagent can be given name and custom instructions
* Agents can communicate with each other, horizontally within team, up/down one level through creation chain
* Synchronous messages (agent session is blocked until reply is recieved), asynchronous inbox/outbox, multicast (ask multiple opinions in team)
* Upper agent can inspect what subordinates are doing
* Few "root" agents (maybe 1), communicate with user through CLI
* Agents are self-organizing into "Managers", "Workers". Manager can run 1 agent to write the code, multiple agents for review, etc.

### Workspaces

* Work is organized in isolated workspaces, managed independently from the agents
* Isolated using bwrap, optional access to network
* Agents can use native provider tools in the workspace
* Symlinks (RO, RW) into external dirs (e.g. project repo)
* Multiple agents can use same workspace (potentially with different permissions)
* Hierachical workspaces, agents can mark a subfolder in their workspace as a workspace for a launched team
* Symlinks within the workspace, link something from your workspace to the agent workspace
* Agents maintain long term context via files/todo in workspace (other tools TBD)
