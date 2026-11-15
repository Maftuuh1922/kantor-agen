# Kantor Jasa

A 3D office of AI staff for a small service business such as a salon, workshop,
tutoring centre or AC servicing: a booking clerk, a billing clerk and a follow-up
clerk work on their own inside the rules you set.

Run it with one command from the repository root:

    .venv/bin/python apps/kantor-jasa/app.py --port 8001

Open <http://127.0.0.1:8001/>. (The default port is `8000`. Use `--port 8001` to run
it next to Kantor Warung.)

* **First run:** the page asks for your business name, hires the three AI staff
  (Dewi for booking, Agus for billing, Rina for follow-up), loads demo data (8 customers,
  about two weeks of services and invoices, tomorrow's agenda), and then asks for the
  daily spending limit and the actions to block. The staff start working after you save
  the rules.
* **What you will see:** Dewi confirms booking requests and moves a clashing one to the
  next free slot. Agus drafts an invoice for each finished service and approves it when it
  is at most Rp 500.000; a larger one waits for you. Rina queues thank-you messages and
  payment reminders in the outbox. Nothing is sent: messages stay pending.
* **Next runs:** the same command opens the office straight away. Your data is in
  `kantor-jasa.db` in the folder you started from.
* **No API key needed:** without `KANTOR_LLM_API_KEY` the AI text comes from an offline
  stand-in. The key is read from the server environment or `.env` and is never sent to
  the browser. New booking requests and payments come from a built-in simulator.

**Demo hours.** By default the booking simulator sends requests at ANY hour (the requested time
still lies inside the working hours), so the office is lively at night too. With a short
`--interval` (8 to 30 seconds) a new request arrives about every 2 to 3 ticks, and the simulator
rests while 30 or more bookings are already waiting or upcoming. `--real-hours` (or
`KANTOR_REAL_HOURS=1`) brings back the 09:00 to 17:00 limit and the slow default arrival rate.

Options:

| Option | Default | Meaning |
|---|---|---|
| `--port` | `8000` | port to listen on |
| `--db` | `kantor-jasa.db` | SQLite file, created on first run |
| `--interval` | `KANTOR_HEARTBEAT_SECONDS`, else `300` | seconds between heartbeat ticks. Use `--interval 10` for a lively demo |
| `--no-pacing` | pacing on | turn off work pacing, so tasks finish at once (see `docs/pacing.md`) |
| `--pace-min` / `--pace-max` | `3` / `8` | shortest and longest think time per task in seconds (`KANTOR_PACING_MIN`, `KANTOR_PACING_MAX`) |
| `--real-hours` | demo hours | keep the working hours (09:00 to 17:00) for new booking requests, so the staff are idle at night (`KANTOR_REAL_HOURS=1`) |
| `--host` | `127.0.0.1` | this computer only. There is no login, so do not expose it to a network |

A database file belongs to one business type: Kantor Jasa refuses a file made by Kantor
Warung, and the other way round. Use a different `--db` for each.

The folder name has a hyphen, so it is not importable. The importable code lives in
`apps/kantor_jasa/` (`template.py`, `seed.py`, `roles.py`, `data.py`, `schema.py`,
`app.py`); `app.py` here only adds the repository root to `sys.path` and calls it.
