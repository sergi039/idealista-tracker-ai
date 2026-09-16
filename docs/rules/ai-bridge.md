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

- **The bridge signs with one Claude profile and falls back to the other on
  an account refusal** (2026-09-16). The owner holds two Claude Max accounts,
  each a Claude Code profile (`~/.claude`, `~/.claude-b`; the Skills repo's
  `docs/claude-multiprofile.md`). On the mini the LaunchAgent starts the bridge
  through the `claude-b` wrapper, which exports `CLAUDE_CONFIG_DIR=~/.claude-b`
  and pins profile B as the PRIMARY. Measured on 2026-09-16: B answered
  `429 You've hit your weekly limit · resets Sep 18`, profile A on the same
  machine answered OK, and the only way to reach A was a second bridge started
  by hand on another port. So `AI_BRIDGE_CLAUDE_FALLBACK_CONFIG_DIRS` (colon-
  separated; the literal `default` is the CLI's own profile with
  `CLAUDE_CONFIG_DIR` unset) names the profiles `complete_claude` tries next,
  in order, and ONLY on an ACCOUNT refusal: a 429, "weekly limit", "usage
  limit", "not logged in", an expired OAuth session. A refusal about the
  request (a 400, a schema the CLI rejects) is answered from the primary and
  never re-spent on the second subscription. Every fallback is logged with the
  profile's directory and the CLI's own words, never a token, and the answer
  carries `claude_profile` so the app's log can say which subscription served
  it. The fallback moves `CLAUDE_SECURESTORAGE_CONFIG_DIR` together with
  `CLAUDE_CONFIG_DIR` -- the keychain namespace follows the config directory,
  and reading A's config against B's stored session is "Not logged in".
  Setting the variable is the launcher's job (`.env`, which
  `run-idealista-ai-bridge-b` / `tools/run_ai_bridge.sh` source), and a
  running bridge reads it only at the next start. Two things it does not do:
  it does not probe a profile before a request (a probe is a spent call), and
  it does not touch codex, which is one ChatGPT account on both machines.
  `tests/test_ai_bridge_claude_profiles.py` runs the real `_run` against a fake
  `claude` on disk and asserts the environment each attempt actually saw.
