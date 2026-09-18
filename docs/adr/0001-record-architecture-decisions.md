# ADR-0001: Record architecture decisions

Date: 2026-09-18
Status: Accepted

## Context

This tracker is developed in short irregular sessions, now with a Portugal
fork of the Spain app in the same tree. Operational rules already live in
`docs/rules/` and `CLAUDE.md`. Those files say how to behave; they do not
record why a one-way choice was made, what was rejected, or which later ADR
supersedes it. Context evaporates between sessions.

## Decision

We will record significant decisions as ADRs in `docs/adr/`, using a MADR-lite
template (Context / Decision / Consequences / Alternatives), numbered
sequentially, immutable once accepted, in English.

## Consequences

- Future sessions (human or AI) can reconstruct "why" without re-deriving it.
- `docs/rules/` remains the Spain operational contract; ADRs do not replace it.
- Small writing overhead per significant decision; trivia stays out of scope.
- A freeze-Decision commit gate like leiloes-radar's can follow; this ADR does
  not install one.
