# Architecture Decision Records

Log of significant decisions. One file per decision. Number sequentially.

Same shape as leiloes-radar: MADR-lite, English, immutable once accepted.
`docs/rules/` stays the operational contract for the Spain app; ADRs record
choices that are hard to reverse or that someone would ask about six months
later. Trivia stays out.

## When to write an ADR

Write one when a decision is hard to reverse, touches dependencies / security /
structure, or when someone would plausibly ask "why is it like this?" later.
Skip trivia.

## Process

1. Copy `template.md` to `NNNN-short-slug.md`.
2. Status: `Proposed` → `Accepted` → `Superseded by ADR-NNNN` | `Deprecated`.
3. Name the ADR in the implementing commit.
4. Do not rewrite an accepted `## Decision`. Add a new ADR and change the old
   file's Status (and links) to point at it.
5. Add the file to the index below.

## Index

- [ADR-0001](0001-record-architecture-decisions.md) — Record architecture decisions
- [ADR-0002](0002-import-favorites-from-logged-in-dumps.md) — Import Idealista.pt favorites from logged-in dumps
