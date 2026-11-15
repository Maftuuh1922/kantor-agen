# Kantor Warung

A 3D office of AI staff for a warung or small shop: a cashier, a stock keeper, a
finance clerk and a marketer work on their own inside the spending limit you set.

Run it with one command from the repository root:

    .venv/bin/python apps/kantor-warung/app.py --port 8000

Open <http://127.0.0.1:8000/>.

* **First run:** the page asks for your shop name and business type, hires the four
  AI staff, loads demo data (12 products and a week of sales), and then asks for the
  daily spending limit and the actions to block. The staff start working after you
  save the spending rules. Nothing can be spent before that.
* **Next runs:** the same command opens the office straight away. Your data is in
  `kantor-warung.db` in the folder you started from.
* **No API key needed:** without `KANTOR_LLM_API_KEY` the AI text comes from an offline
  stand-in. The key is read from the server environment or `.env` and is never sent to
  the browser.

**Demo hours.** By default the simulated customers come at ANY hour, so the office is lively
when you open it at night, and a stock delivery takes about a minute instead of 2 hours.
With a short `--interval` the simulated sales are scaled down (about one sale a minute at most),
so the shelves do not empty in a quarter of an hour. `--real-hours` (or `KANTOR_REAL_HOURS=1`)
brings back the real shop hours and the 2-hour delivery.

Options:

| Option | Default | Meaning |
|---|---|---|
| `--port` | `8000` | port to listen on |
| `--db` | `kantor-warung.db` | SQLite file, created on first run |
| `--interval` | `KANTOR_HEARTBEAT_SECONDS`, else `300` | seconds between heartbeat ticks. Use `--interval 10` for a lively demo |
| `--no-pacing` | pacing on | turn off work pacing, so tasks finish at once (see `docs/pacing.md`) |
| `--pace-min` / `--pace-max` | `3` / `8` | shortest and longest think time per task in seconds (`KANTOR_PACING_MIN`, `KANTOR_PACING_MAX`; `KANTOR_PACING=0` turns pacing off) |
| `--real-hours` | demo hours | keep the shop hours (07:00 to 21:00) and a 2-hour delivery, so the staff are idle at night (`KANTOR_REAL_HOURS=1`) |
| `--host` | `127.0.0.1` | this computer only. There is no login, so do not expose it to a network |

The folder name has a hyphen, so it is not importable. The importable code lives in
`apps/kantor_warung/` (`template.py`, `seed.py`, `roles.py`, `data.py`, `app.py`);
`app.py` here only adds the repository root to `sys.path` and calls it.
