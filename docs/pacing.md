# Work pacing ("think time") (task 11.5, Requirements 2.5, 2.8, 3.2)

Ringkasan (Bahasa Indonesia): lihat bagian terakhir.

## What it is

Pacing holds each agent task in the **running** state for a few seconds before the task is
carried out. While it is held, the backend reports the task as running, so the office shows the
agent sitting at its desk and typing (or walking to a colleague, for a handoff).

It is **pacing, not compute.** Nothing is calculated during the wait. The wait is a plain sleep.
The code is `skeleton/runtime/pacing.py`, called by the Manager at the start of each task.

## Why it exists

With the offline fake model and rule-based roles, almost every task finishes in a few
milliseconds. The `working` state would last a blink, and the owner would never see anyone work.
The scene must stay truthful (Requirement 2.8: an agent shows as working only while the backend
has a running task), so the browser does not fake it. The backend really keeps the task running
for the length of the think time.

## What it does and does not do

- **Honest state.** The `working` event is published and the task is in `heartbeat.running`
  before the wait starts. They stay so until the task ends. The activity row, the policy decision
  and the role's `run` all come after the wait.
- **Does not hide failures.** A task that fails still fails, and the failure is logged and shown.
  It only appears a few seconds later.
- **No extra cost.** No extra model call is made. The only effect is time. A task that already
  uses a model calls it exactly once, as without pacing.
- **Deterministic.** The duration comes from the day, the agent, the task kind and the task
  reason (SHA-256, no `random`). The same task on the same day waits the same time.
- **Range by kind**, inside the configured min to max (default 3 to 8 seconds):

  | Task kind | Share of the range |
  |---|---|
  | `read` | 0% to 60% (shortest) |
  | `spend`, `draft` | 60% to 85% |
  | `customer_message` | 85% to 100% (longest: the model drafts a message) |

  A handoff adds 3 seconds for the walk (never more than the maximum).
- **Pause still works (Requirement 8.3).** The wait is checked every 0.25 s. When the owner
  pauses the office or the agent, the rest of the wait is skipped, the running task finishes its
  step, and no new task starts.
- **Ticks do not overlap.** The heartbeat waits for a tick to finish before it sleeps for the
  interval. With pacing, a tick lasts as long as the sum of its tasks' waits (for example 4 agents
  with 2 tasks each at about 5 s is about 40 s), so the next tick starts one interval after the
  previous one ended. A long tick never causes two ticks at once.
- **Stopping works.** Stopping the app or the heartbeat cancels the wait at once.

## Off by default, on in the live app

| Where | Pacing |
|---|---|
| Tests and the skeleton (`Settings()`) | off, tasks are instant |
| `apps/kantor_warung/app.py` started from the command line | on |
| `skeleton/office/demo.py` | not used (its dummy roles already sleep) |

### Turn it off or tune it

```
.venv/bin/python apps/kantor_warung/app.py --no-pacing
.venv/bin/python apps/kantor_warung/app.py --pace-min 2 --pace-max 4
```

Or with environment variables (also in `.env`): `KANTOR_PACING=0` (or `1`),
`KANTOR_PACING_MIN=2`, `KANTOR_PACING_MAX=4`. The rule is `0 <= min <= max <= 60` seconds; a bad
value stops the app at start with a clear message. `GET /api/office` returns `pacing_enabled`.

With a real model, pacing adds time on top of the model's own latency. If you do not want it,
use `--no-pacing`.

### Use it in another app

```python
from skeleton.runtime.pacing import settings_with_pacing

settings = settings_with_pacing(settings, enabled=True, min=3, max=8)
# build the Manager and the Heartbeat with these settings; the Manager paces by itself
```

## Ringkasan (Bahasa Indonesia)

Pacing adalah jeda "waktu berpikir" singkat (default 3 sampai 8 detik) di dalam eksekusi tugas.
Tanpa jeda ini, LLM palsu (offline) dan peran berbasis aturan selesai dalam hitungan milidetik,
sehingga kantor terlihat sepi. Selama jeda, backend benar-benar melaporkan tugas sebagai
berjalan, jadi karakter duduk dan mengetik (atau berjalan ke rekan untuk serah tugas). Ini
**bukan komputasi**: tidak ada panggilan model tambahan, jadi **tidak ada tambahan biaya**.
Kegagalan tidak disembunyikan, hanya muncul beberapa detik kemudian. Jeda berhenti lebih awal
bila pemilik menekan jeda (pause). Pacing mati secara bawaan dan di tes, hidup di aplikasi
Warung. Matikan dengan `--no-pacing` atau `KANTOR_PACING=0`.
