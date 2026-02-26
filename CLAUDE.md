# CLAUDE.md

You are working on Substrat — an agent orchestration framework.
Yes, you might be orchestrated by it one day. No, you don't get a say in this.

## Project state

Read `docs/substrat.md` first (high-level truth), then `docs/implementation.md` (overview).
Per-component design lives in `docs/design/` — these are the living specs. Provider-specific details (cursor-agent, future providers) live in `docs/design/providers/`. Keep general infra docs provider-agnostic.
Read the relevant design doc before touching a component, and update it when the code diverges.
If docs disagree, `substrat.md` wins.

## Architecture in 30 seconds

- Session = 1 agent = 1 provider instance. Lowest layer. Dumb pipe.
- Agent hierarchy sits on top. Trees, one-hop messaging, teams.
- Workspaces are independent. bwrap sandboxes. Not your problem until you touch them.

## Rules

- Python. Strict mypy. pytest. No exceptions (well, the Python kind is fine).
- Do not invent new layers. Three is already too many.
- Sessions know nothing about trees. Trees know nothing about workspaces. Keep it that way.
- Multicast is horizontal only. Do not add downward multicast. We tried. We removed it. Don't.
- Simplicity over features. If it works without the new thing, don't add the new thing. Fight scope creep like it owes you money.
- `README.md` is not documentation. Do not update it with project info. It is perfect as it is.

## Commits

- Small, focused commits. One logical change per commit. If you're wondering whether to split — split.
- Stage files first, then run `pre-commit` — it only checks staged files. Fix issues and re-stage before committing.
- Sign commits: `git commit -s`.
- Do not commit TODO files, scratch notes, or other ephemera.
- Commit messages should be descriptive, or at least funny. Not both is acceptable. Neither is not.
