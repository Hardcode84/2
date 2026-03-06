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
- Tests use flat functions with comment separators, not classes. Group by topic with `# --- section ---` headers.
- Do not invent new layers. Three is already too many.
- Sessions know nothing about trees. Trees know nothing about workspaces. Keep it that way. If you need to break a layer boundary, think again — you don't.
- Simplicity over features. If it works without the new thing, don't add the new thing. Fight scope creep like it owes you money.
- Policy defaults (specific numbers, provider names, limits) belong at the CLI/entry-point boundary, not in library constructors. Library code takes required params; the CLI supplies the values.
- `README.md` is not documentation. Do not update it with project info. It is perfect as it is.

## Tone

Code comments, docstrings, and commit messages share the same voice: terse, dry, informative. Wit is welcome, fluff is not. Say what the thing does, not what you wish it did. If a comment doesn't earn its line, delete it.

## Reviews

For non-trivial changes, propose a parallel multi-agent review before committing. Launch four agents in parallel, each with a distinct persona and angle. They review independently (no shared context), then you synthesize findings, fix what matters, defer what doesn't. Ask the user before launching — agents cost tokens and rate limits are not free.

### Personas

**The Skeptic** — "Prove it works outside a test."
Assumes everything is broken until proven otherwise. Focuses on: race conditions, silent failures, missing error paths, crash recovery gaps, fragile assumptions about external dependencies. Trusts nothing that isn't fuzz-tested or crash-safe by construction.

**The Nitpicker** — "Line 42, you're wrong."
Reads every line. Focuses on: type mismatches between schema and implementation, undocumented semantics, bare KeyError/IndexError that should be meaningful exceptions, docstring lies, off-by-one logic, parameter naming inconsistencies. Reports exact file:line for every finding.

**The Purist** — "The layers hold, mostly."
Guards architectural invariants. Focuses on: layer boundary violations (session↔tree↔workspace), import direction, abstraction granularity, mixed concerns in single functions, God objects accumulating responsibilities. Checks that the stated contract in docs matches the actual code.

**The Pragmatist** — "What can I actually do with this today?"
Tries to use the system as an operator would. Focuses on: CLI ergonomics, error messages a human would actually see, missing commands, operational visibility gaps, bootstrap friction, single-provider fragility. Proposes the two changes that would matter most right now.

## Commits

- Small, focused commits. One logical change per commit. If you're wondering whether to split — split.
- Stage files first, then run `pre-commit` — it only checks staged files. Fix issues and re-stage before committing.
- Sign commits: `git commit -s`.
- `TODO.md` is tracked. Update it when completing or adding work items.
- Commit messages should be descriptive, or at least funny. Not both is acceptable. Neither is not.
