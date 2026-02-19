# Dev Rules (Ports & Isolation)

This repository is a **separate universal build** and must be runnable side-by-side with the legacy `IdealistaRank` project without clobbering it.

## Hard Rules

- Legacy project (`IdealistaRank`) stays on: `http://localhost:5001/`
- Universal project (`IdealistaRank-properties-universal`) stays on: `http://localhost:5050/`

## Docker Isolation (must stay unique)

Universal build must use **unique** Docker resources (do not reuse legacy names):

- Containers: `idealista-universal-app`, `idealista-universal-db`
- Network: `idealista-universal-network`
- Volume: `idealista-universal-pgdata`
- Host ports:
  - App: `5050 -> 5001` (container)
  - DB debug: `5434 -> 5432` (container)

## Data Isolation (recommended)

To avoid mixing email sources when running both apps:

- Use different IMAP folders/labels (e.g. `IMAP_FOLDER=Idealista` in legacy vs `IMAP_FOLDER=IdealistaProperties` in universal)
- Use different DB names in `.env` (`DB_NAME=idealista` vs `DB_NAME=idealista_universal`)

## Browser Session Isolation (recommended)

Cookies do **not** isolate by port on `localhost`, so run the universal build with a different cookie name:

- Universal default: `SESSION_COOKIE_NAME=idealista_universal_session`
