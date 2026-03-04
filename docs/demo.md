# Quick demo

Requires `cursor-agent` in PATH.

## Start the daemon

```bash
python -m substrat daemon start
python -m substrat daemon status
```

## Create an agent and talk to it

```bash
python -m substrat agent create scout \
  --instructions "You are a terse assistant. Answer in one sentence."
# prints agent_id hex — copy it

python -m substrat agent send <AGENT_ID> "What is the capital of Mongolia?"

# verify context persists across turns
python -m substrat agent send <AGENT_ID> "What did I just ask you about?"
```

## Inspect and clean up

```bash
python -m substrat agent inspect <AGENT_ID>
python -m substrat agent list
python -m substrat agent terminate <AGENT_ID>
```

## Multi-agent: parent spawns a child

The parent agent sees `spawn_agent`, `check_inbox`, and other tools
injected into its system prompt. When it emits `<tool_call>` tags,
the daemon dispatches them and feeds results back.

```bash
python -m substrat agent create boss \
  --instructions "You are an orchestrator. When asked to research something, \
use spawn_agent to create a child named 'researcher' with instructions to \
find the answer and call complete(result) when done. Then wait — you will \
receive the child's result as a message automatically."
# copy BOSS_ID

python -m substrat agent send <BOSS_ID> "Research: what year was Ulaanbaatar founded?"
# boss should spawn a child, child completes, boss gets woken with the answer

python -m substrat agent list
# should show boss (idle) — child self-terminated via complete()

python -m substrat agent terminate <BOSS_ID>
```

## Stop the daemon

```bash
python -m substrat daemon stop
```
