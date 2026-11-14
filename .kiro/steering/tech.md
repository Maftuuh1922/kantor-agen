# Kantor Agen: Tech

## Stack

- Backend: Python 3.11+, FastAPI, uvicorn, httpx.
- Storage: SQLite, one file per application.
- Front end: Three.js (vendored `three.module.js`), glTF characters and furniture with CC0 licenses.
- Live updates: REST for commands, SSE for state.
- Tests: pytest and Hypothesis. Run with `.venv/bin/pytest -q`.
- Model API: any OpenAI-compatible endpoint, configured in `.env` (see `.env.example`).

## Layout

- `skeleton/`: shared core.
  - `core/`: config, database, event bus, clock.
  - `policy/`: policy engine and spend ledger.
  - `runtime/`: heartbeat, manager, agent base class, LLM client.
  - `log/`: activity log and daily summary.
  - `office/`: FastAPI app factory, SSE stream, static front end.
- `apps/kantor-warung/` and `apps/kantor-jasa/`: roles, triggers, models, seed data, `app.py`.
- `tests/`: property-based and unit tests.
- `.kiro/`: steering, specs, hooks. Keep it tracked in git.
- `CREDITS.md`: source and license of every 3D asset.

## Runtime

Single process. A heartbeat loop evaluates agent triggers with rules first, and calls the model only for text or judgment.
