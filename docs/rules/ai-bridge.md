# The AI bridge

Moved verbatim from `CLAUDE.md` (lines 2539–2556 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

- **The AI bridge runs both CLIs cold** (#201). `tools/ai_bridge.py` reaches
  Claude and ChatGPT through the owner's *subscriptions*, and both CLIs are
  agents: left with their defaults they read the owner's personal config, load
  the repository they were started in, and go to work. Measured, that meant a
  listing valuation taking 4m50s and hitting the 600 s timeout, `multi_agent`
  spawning research sub-agents, a 1100-token prompt arriving as 57k input
  tokens, and claude carrying 21 KB of this file into every valuation. So codex
  gets `--ignore-user-config --ignore-rules --ephemeral`, every interactive
  feature `--disable`d, and `model_reasoning_effort=low`; claude gets
  `--safe-mode --tools "" --effort low --no-session-persistence`; both run in an
  empty `workdir()` outside any repository. Do not restore a *profile* instead
  of `--ignore-user-config` — profiles layer on top of the base user config, so
  anything the owner adds later reaches this service. Do not set a fast/priority
  service tier: it buys 1.5x speed for 2.5x the credit rate, on every listing.
  A timeout kills the whole **process group**, because `codex` is a node wrapper
  whose grandchild does the work and survives a kill aimed at the wrapper —
  measured, five extra minutes of billed work nobody read.
  `tests/test_ai_bridge_isolation.py` fails if any of that is undone.
