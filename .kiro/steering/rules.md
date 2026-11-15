# Kantor Agen: Rules

These rules apply to all code in this repository.

- **Money is integer rupiah.** Store and compute amounts as `int`. Never use float for money.
- **Policy checks live in backend code only.** The daily limit and blocked-action list are enforced by the policy engine in a single SQLite transaction. Never rely on a model prompt to enforce a limit.
- **Customer messages go to the outbox.** Write them with status `pending`. Never add code that sends them in the MVP.
- **Never commit secrets.** The API key comes from the server environment (`.env`). `.env` stays in `.gitignore`. `.kiro/` must not be in `.gitignore`.
- **The skeleton never imports from `apps/`.** Apps import from `skeleton/`, not the other way round.
- **The backend decides visual state.** The browser renders the `VisualState` it receives and never shows an agent as working unless the backend reports a running task.
- **Stock is never negative.** Reject any change that would make a quantity below zero.
- **The activity log is append-only.** No edit or delete path in the UI or API.
- **Every 3D asset needs a redistributable license** and an entry in `CREDITS.md`.
