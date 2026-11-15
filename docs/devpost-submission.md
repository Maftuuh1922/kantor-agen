# Devpost submission text (Kiroween 2026, Skeleton Crew)

Ready to paste. Numbers were checked on commit `a600edf` of branch `kantor-agen/build` (task 13.3 audit). Anything marked `TODO(owner)` needs you. Evidence for every Kiro claim is in [KIRO_USAGE.md](KIRO_USAGE.md).

## Project name

Kantor Agen

## Elevator pitch (under 200 characters)

A 3D office of AI staff for small businesses. They work on their own inside limits you set once, and backend code, not a prompt, enforces those limits. One skeleton, two apps.

## Category

Skeleton Crew

## Project description (about 250 words)

Kantor Agen is a 3D office for small business owners in Indonesia (UMKM). You pick a business type, and a team of AI employees sits down at their desks. A shop gets a cashier, stock keeper, finance clerk and marketer (Kantor Warung). A service business gets booking, billing and follow-up clerks (Kantor Jasa).

The staff act on their own. A heartbeat checks each agent's rules on a schedule, and a language model is called only when a task needs written text. A Manager Agent routes handoffs and picks the model per task. The owner sets a daily spending limit and a list of blocked actions once. A policy engine in backend code checks every spend and message before it runs, so a model output alone cannot get past the limit. Customer messages go to an outbox and are never sent. Every action is written to an append-only activity log, and the owner can pause the whole office or a single agent.

The staff are rigged 3D characters that walk around furniture, sit and type, walk to a colleague to hand off work, and raise a hand when they wait for the owner. Over-limit orders and large invoices wait in an approval queue (Setujui or Tolak). An approval still passes the policy engine, and a rejected order is not asked again for 24 hours. The backend decides what each character does, so someone appears to work only while a task is really running.

Skeleton Crew angle: one skeleton (policy engine, runtime, Manager, activity log, 3D office, onboarding, API) powers two apps in two folders, `apps/kantor-warung` and `apps/kantor-jasa`. Each app adds roles, data, seed data and a template.

The demo needs no API key. Known limits: no real messages or payments, and frame-rate and load-time targets are not yet measured on a real laptop.

## How I used Kiro (about 300 words)

I built Kantor Agen with the Kiro IDE spec workflow, and I checked every claim below against the repository.

**Spec first.** Before any code I committed 11 requirements, a design with 7 correctness properties, and a task plan with a dependency graph. The plan ran in waves: tasks that do not depend on each other were handed to task-execution agents by an orchestrating Kiro session, with checkpoints (tasks 6, 9 and 12) where I reviewed the result. Checkpoint 12 led to real fixes. The plan also changed during the build (tasks 8.9 and 11.5 were added), and Kiro's `tasks.meta.json` records the execution history.

**Steering.** Three files in `.kiro/steering/` set the rules: money is integer rupiah, policy checks live in backend code only, customer messages stay in the outbox, the skeleton never imports from the apps. The code follows them, and tests enforce several (a `CHECK` constraint on the outbox, a test that the skeleton names no business type).

**Hook.** One hook runs the test suite when a Python file under `skeleton/` or `apps/` is saved. The full suite takes about 7 minutes, which is too slow for that job. I would narrow it.

**Property-based tests.** Seven Hypothesis properties from the design, 200 examples each, cover stock, sale totals, the daily limit, blocked actions, outbox-only messages, pause, and truthful visual state. A later file covers the approval queue. I did not find a bug that only a property test caught. In seven hand-made mutations (for example, removing the limit check), made on an earlier commit (`4b96e5d`), the property tests and the unit tests both failed.

**Not used.** No MCP server, no custom agent and no Kiro power. I did not use Kiro CLI, Web or Crew for this project. `TODO(owner)`: confirm this sentence is true before submitting.

Full write-up with file links: `docs/KIRO_USAGE.md`.

## What I learned

Writing the rule "the scene shows work only while the backend reports a running task" into the spec first made it one pure function and one property test, instead of a UI detail. The spec drifted afterwards: the design still shows `cost: Decimal` while the code uses integer rupiah, and Property 7 gained a handoff condition in code that the design text lacks. Property tests did not find more than the unit tests here, but they state each guardrail independent of the implementation. The slow on-save hook taught me to match a hook's scope to its cost.

## Built with

- Kiro IDE (specs, steering, hooks)
- Python 3.11+, FastAPI, uvicorn, httpx
- SQLite
- JavaScript, Three.js r187 (vendored), glTF
- pytest, Hypothesis, Node test runner
- Any OpenAI-compatible model API (optional; the demo uses an offline stand-in)
- CC0 assets: Quaternius Universal Base Characters and Animation Libraries, Kenney Furniture Kit (see `CREDITS.md`)

## Submission checklist

| Field | Status |
|---|---|
| Category: Skeleton Crew, two app folders in one repo | `apps/kantor-warung`, `apps/kantor-jasa` (verified) |
| Public repository URL | `https://github.com/Maftuuh1922/kantor-agen`. `gh repo view` reported `PUBLIC` (visibility public, not private) at the task 13.3 audit |
| Default branch has the work | At the audit `main` held only the three spec files and `LICENSE` (the default branch on GitHub is `main`), and the build was on `kantor-agen/build`. `TODO(owner)`: after the merge, open the repository page and confirm that `README.md`, `.kiro/`, `apps/` and `skeleton/` show on `main` |
| Open source license visible | MIT, `LICENSE` at the root. GitHub's license detection reports `MIT License` (`gh repo view --json licenseInfo`). `TODO(owner)`: after the merge, confirm "MIT license" shows in the About box |
| `.kiro/` at the root, tracked, not in `.gitignore` | Verified on `kantor-agen/build` (8 tracked files: 3 steering, 1 hook, 4 spec files). `git check-ignore` matched none of them |
| Kiro write-up | `docs/KIRO_USAGE.md` |
| Root `README` with run instructions | `README.md` exists, with five screenshots in `docs/images/`. The app READMEs exist too |
| No secrets in the history | Scanned all branches of the git history at `a600edf` (task 13.3): no real key or token. The only matches are test canaries (`sk-FAKE-...`) and the placeholders in `.env.example`. No `.env`, `.db` or key file was ever committed |
| Demo video, public YouTube URL, under 3 minutes | `TODO(owner)` |
| Social media post URL | `TODO(owner)` |
| Blog post URL | `TODO(owner)` |
| Screenshots | `TODO(owner)` |
| Team members | `TODO(owner)` |
| Testing instructions | see below |
| Submission deadline | `tasks.md` says 4 December 2026, 14:00 Pacific Time. `TODO(owner)`: confirm on the Devpost page |

**Testing instructions (paste into the form):**

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python apps/kantor-warung/app.py --port 8000    # then open http://127.0.0.1:8000/
.venv/bin/python apps/kantor-jasa/app.py --port 8001      # then open http://127.0.0.1:8001/
.venv/bin/pytest -q                                       # 1335 tests, about 7 minutes (two of them run the Node suite)
node --test tests/js/                                     # 144 tests
```

On the first run the page asks for a business name, then for the daily spending limit; the staff start after you save it. For a lively first look add `--interval 10`. To see the approval queue, set a low daily limit (for example Rp 20.000) in Aturan.

No API key is needed; without `KANTOR_LLM_API_KEY` the app uses an offline stand-in model. Do not claim 30 fps or a 5 second load time anywhere: [performance.md](performance.md) says neither was verified on real hardware.
