# Quick demo

Requires `cursor-agent` in PATH.

## Start the daemon

```bash
substrat daemon start
substrat daemon status
```

## Create an agent and talk to it

```bash
substrat agent create scout \
  --instructions "You are a terse assistant. Answer in one sentence."
# prints agent_id hex — copy it

substrat agent send <AGENT_ID> "What is the capital of Mongolia?"

# verify context persists across turns
substrat agent send <AGENT_ID> "What did I just ask you about?"
```

## Inspect and clean up

```bash
substrat agent inspect <AGENT_ID>
substrat agent list
substrat agent terminate <AGENT_ID>
```

## Multi-agent: parent spawns a child

The parent agent sees `spawn_agent`, `check_inbox`, and other tools
injected into its system prompt. When it emits `<tool_call>` tags,
the daemon dispatches them and feeds results back.

```bash
substrat agent create boss \
  --instructions "You are an orchestrator. When asked to research something, \
use spawn_agent to create a child named 'researcher' with instructions to \
find the answer and call complete(result) when done. Then wait — you will \
receive the child's result as a message automatically."
# copy BOSS_ID

substrat agent send <BOSS_ID> "Research: what year was Ulaanbaatar founded?"
# boss should spawn a child, child completes, boss gets woken with the answer

substrat agent list
# should show boss (idle) — child self-terminated via complete()

substrat agent terminate <BOSS_ID>
```

## Watch live events

In a separate terminal, tail all session event logs as they happen:

```bash
substrat daemon watch
```

Then interact with agents in another terminal — watch prints each event
as it lands:

```
14:02:31  a3f8  session.created  provider=cursor-agent model=claude-sonnet-4-5-20250514
14:02:31  a3f8  turn.start  prompt=What is the capital of Mongolia?
14:02:33  a3f8  turn.complete  response=The capital of Mongolia is Ulaanbaatar.
```

Filter to a single agent with `--agent-id`:

```bash
substrat daemon watch --agent-id <AGENT_ID>
```

## Stop the daemon

```bash
substrat daemon stop
```
