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

## Stop the daemon

```bash
python -m substrat daemon stop
```
