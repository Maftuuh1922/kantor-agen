# Demo video guide (Kiroween 2026, Skeleton Crew)

For the owner. Nothing here is recorded yet. The Devpost video must be **public on YouTube, under 3:00**, and must show the project running on the machine it was built for. The plan below is 2:30, which leaves 30 s of slack.

Every number in this guide was measured on a fresh database (task 13.3, commit `a600edf`, Linux): the three first stock orders cost Rp 92.000 (Kecap), Rp 216.000 (Susu) and Rp 232.500 (Gula).

## 1. Start the apps

Run from the repository root, in a terminal with a short prompt (see tips).

Linux and macOS:
```
.venv/bin/python apps/kantor-warung/app.py --port 8000 --interval 8 --pace-min 2 --pace-max 4
.venv/bin/python apps/kantor-jasa/app.py   --port 8001 --interval 8 --pace-min 2 --pace-max 4
```
Windows (PowerShell):
```
.venv\Scripts\python apps\kantor-warung\app.py --port 8000 --interval 8 --pace-min 2 --pace-max 4
.venv\Scripts\python apps\kantor-jasa\app.py   --port 8001 --interval 8 --pace-min 2 --pace-max 4
```
Open `http://127.0.0.1:8000/` (Warung) and `http://127.0.0.1:8001/` (Jasa). `--interval 8` makes the staff wake every 8 s, and `--pace-min 2 --pace-max 4` keeps each task "running" for 2 to 4 s, so work is visible.

**Which limit to type.** Use **`100000`** (the dialog's own default). I tested the other options:

| Daily limit | What happens in the first 15 s |
|---|---|
| `100000` (use this) | The Kecap order (Rp 92.000) fits: Budi hands it to Wati ("Serah tugas"). Gula and Susu do not fit, so two requests wait. One run shows the handoff, the amber wait and the queue. |
| `60000` or `20000` | All three orders wait (3 in the queue). No handoff, because nothing fits. Use this only for a take that shows the queue alone. |

## 2. Timed outline (2:30)

Narrate in English, or in Indonesian with **English captions** (the rules want English materials or an English translation). Keep sentences short.

| Time | Scene | What to do on screen | Say (one idea) |
|---|---|---|---|
| 0:00-0:12 | Problem | Show the already-running office from a fresh start, or the onboarding page. | "A small shop owner cannot watch an AI agent all day, and cannot afford one that overspends." |
| 0:12-0:30 | Onboarding | Type a shop name (for example `Warung Bu Tini`), keep the business type, press **Buka kantor**. The four hired staff appear. | "Pick a business, and the staff are hired with demo data." |
| 0:30-0:45 | Set the policy | In **Atur aturan belanja** type `100000`, leave blocked actions as they are, press **Simpan dan mulai**. | "The owner sets the daily limit once. It is enforced in backend code, not in a prompt." |
| 0:45-1:05 | Staff work and a handoff | Wait 10 to 15 s. Staff sit and type. Budi walks to Wati. The feed (**Aktivitas**, bottom left) shows "Serah tugas". | "The characters move only when the backend reports a running task." |
| 1:05-1:20 | Amber waiting | A raised hand and the amber **Menunggu keputusan (2)** button in the top bar. | "Anything over the limit waits for me." |
| 1:20-1:50 | Approve over the limit, then raise it | Open the button. Press **Setujui** on the Gula order (Rp 232.500): the card shows a refusal ("Melebihi batas harian Rp 100.000...") and a **Buka Aturan** button. Press it, type `500000`, **Simpan**. Press **Setujui** again: it goes through. Press **Tolak** on the Susu order. | "Approving does not skip the policy. The engine refused it until I raised the limit." |
| 1:50-2:00 | Activity log | Show **Aktivitas**, then click an agent (Budi) to open the panel with **Aksi terbaru**. The "Keputusan pemilik" rows are there. | "Every action and every decision is written to an append-only log." |
| 2:00-2:12 | `.kiro` and specs | On the GitHub repo page (after the merge), show `.kiro/` at the root, then `specs/kantor-agen/`, `steering/`, `hooks/`, and "MIT license" in About. | "Built with Kiro specs, steering and a hook. The repository has all of it." |
| 2:12-2:30 | Skeleton Crew proof | In the file tree show `apps/kantor-warung` and `apps/kantor-jasa` next to `skeleton/`. Switch to the Jasa tab (port 8001): staff working, invoices waiting in the queue. | "One skeleton, two apps: a shop and a service business, in two folders." |

If a take runs past 2:45, cut the problem sentence to one line and shorten the Jasa part. Never go over 3:00. Judges are not required to watch past it.

## 3. How to trigger each moment fast

| Moment | How |
|---|---|
| Handoff | Starts about 10 to 15 s after **Simpan dan mulai** with limit `100000`. |
| Amber waiting and queue | Same moment. Two orders wait: Gula (Rp 232.500) and Susu (Rp 216.000). |
| Over-limit refusal | **Setujui** on Gula while the limit is `100000`. |
| Raising the limit | **Buka Aturan** on the refused card (or **Aturan** in the top bar), `500000`, **Simpan**, then **Setujui** again. The toast says "Aturan disimpan. Tekan Setujui lagi untuk mencoba ulang." |
| Both approvals | `600000` also covers Susu (Rp 540.500 in total). `500000` covers only Gula, so use **Tolak** for Susu. |
| Jasa queue | Start Jasa 1 minute before you need it and finish its onboarding (name, **Buka kantor**, limit `100000`). In my test two invoices of Rp 650.000 were waiting within 45 s, because Jasa approves invoices only up to Rp 500.000 by itself. |
| Another take | Stop the app and delete `kantor-warung.db` (and `kantor-warung.db-wal`, `kantor-warung.db-shm` if present), or start with `--db demo2.db`. |

## 4. Pre-recording checklist

- [ ] Fresh database for each app (see "Another take"). The first run must show onboarding.
- [ ] Rehearse once with a throwaway database, then delete it before the real take.
- [ ] Browser: one window, zoom 100%, 16:9 (full screen, F11). Close every other tab. Hide the bookmarks bar and extensions.
- [ ] URL has no `?perf=1` (no performance overlay). Do not use `?mock=1`.
- [ ] Quality button says **Kualitas: Normal**. If the machine drops frames, switch to **Rendah** before recording; the choice is saved in the browser.
- [ ] Do a dry run of the whole outline and time it with a stopwatch.
- [ ] Screen resolution 1920x1080. Check that text in the approval panel is readable at that size.
- [ ] No `.env` open, no API key on screen. The demo needs no key.
- [ ] No personal names or home paths on screen: use a short prompt (`export PS1='$ '` on Linux, a short working directory on Windows), a generic OS user, and do not show the file manager's path bar.
- [ ] Notifications off (Do Not Disturb). Close chat apps.
- [ ] Microphone test, 10 s clip. No background noise.
- [ ] The repo page you show is the merged `main`, with `.kiro/` visible and "MIT license" in About. If `main` is still not merged, do not record that scene yet.

## 5. Tips

- **Record:** OBS Studio works on Linux and Windows (Display Capture, 1920x1080, 30 fps, MP4). Windows also has Game Bar (Win+G). On Linux the desktop's own screen recorder is fine if it can do 1080p.
- **Show it on the real machine.** Record the screen of the PC that runs the app, not a phone or a recording of a recording.
- **No copyrighted music.** Record with no music at all, or use only music you own or that YouTube's own Audio Library allows. Avoid logos of other companies (browser branding, other apps, sticker-covered laptops); crop to the window.
- **Language:** English or Indonesian. With Indonesian narration, add English captions (YouTube's caption editor or an `.srt` file) before you submit.
- **Length:** target 2:30, never above 3:00. Check the final file length, not the plan.
- **Upload:** set visibility to **Public** (Unlisted is not enough). Paste the link into the Devpost form and the `TODO(owner)` row in [devpost-submission.md](devpost-submission.md).
- **Honest limits:** do not say 30 fps or a 5 second load time ([performance.md](performance.md)). Say that customer messages stay in the outbox and that the sales feed is a simulator.
