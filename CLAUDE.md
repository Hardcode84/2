# CLAUDE.md

You are working on Substrat — an agent orchestration framework.
Yes, you might be orchestrated by it one day. No, you don't get a say in this.

## Project state

Design phase. No code yet, just docs with opinions.
Read `docs/substrat.md` first (high-level truth), then `docs/implementation.md` (how the sausage gets made).
If the two disagree, `substrat.md` wins.

## Architecture in 30 seconds

- Session = 1 agent = 1 provider instance. Lowest layer. Dumb pipe.
- Agent hierarchy sits on top. Trees, one-hop messaging, teams.
- Workspaces are independent. bwrap sandboxes. Not your problem until you touch them.

## Rules

- Python. Strict mypy. pytest. No exceptions (well, the Python kind is fine).
- Do not invent new layers. Three is already too many.
- Sessions know nothing about trees. Trees know nothing about workspaces. Keep it that way.
- Multicast is horizontal only. Do not add downward multicast. We tried. We removed it. Don't.
- `README.md` is not documentation. Do not update it with project info. It is perfect as it is.

## Commits

- Sign commits: `git commit -s`.
- Commit messages should be descriptive, or at least funny. Not both is acceptable. Neither is not.
