# How Kiro was used to build Kantor Agen

**Ringkasan (Bahasa Indonesia).** Kantor Agen dibangun dengan Kiro IDE memakai alur spec: requirements, design, dan daftar tugas ditulis dan di-commit sebelum kode pertama. Tugas dijalankan oleh agen dalam gelombang sesuai grafik dependensi, dengan tiga checkpoint untuk pemilik (tugas 6, 9, 12). Folder `.kiro/` berisi tiga file steering dan satu hook, dan tujuh properti kebenaran diuji dengan Hypothesis. MCP, custom agent, dan Kiro powers tidak dipakai, dan repo tidak menunjukkan pemakaian Kiro CLI, Web, atau Crew (lihat bagian 7). Semua angka dicek pada commit `4b96e5d`.

## 1. Summary

Kantor Agen is a local web app: a 3D office of AI staff for small businesses, entered in the Skeleton Crew category. One skeleton powers two apps, Kantor Warung and Kantor Jasa. Kiro was used through the **Kiro IDE spec workflow** (11 requirements, a design with 7 correctness properties, a task plan), task execution, three steering files, one hook, and property-based tests. All numbers were checked on commit `4b96e5d` of branch `kantor-agen/build`: 20 commits (the initial commit, 4 spec commits, the merge of the spec pull request, 14 code commits) over about 11 hours (14 Nov 15:19 to 15 Nov 02:27). Git cannot separate agent-written from hand-written code, so I make no claim about that split. Open items are marked `TODO(owner)`.

## 2. How Kiro was used (IDE and agent workflow)

- **Spec first.** [requirements.md](../.kiro/specs/kantor-agen/requirements.md), [design.md](../.kiro/specs/kantor-agen/design.md) and [tasks.md](../.kiro/specs/kantor-agen/tasks.md) were committed (`407c339`, `5310aaf`, `2dbd236`, `8d96c02`) before the first code commit (`85acda9`).
- **Tasks in waves.** `tasks.md` has a dependency graph: 15 waves at `4b96e5d`, 17 after task 14 was added. Tasks in one wave are independent. An orchestrating Kiro session handed them to task-execution agents; the repository records execution IDs, not agent identities.
- **Checkpoints.** Tasks 6, 9 and 12 stop and ask the owner. Checkpoint 12 led to real changes in `4b96e5d` (feed readability, a reopenable Rules dialog, button states, sales-feed seeding).
- **Run record.** [tasks.meta.json](../.kiro/specs/kantor-agen/tasks.meta.json), written by Kiro, lists 56 tasks with execution history, 11 execution IDs and one chat session. It records a passed status for two of the seven property-test tasks (3.3 and 10.3).
- **The plan changed.** Tasks 8.9 (diverse character looks) and 11.5 (visible work pacing) first appear in `tasks.md` in the commits that implemented them (`e466cec`, `463886e`).

## 3. Spec-driven development

One requirement, traced through the chain:

| Step | Where |
|---|---|
| Requirement 2.8: an agent shows as working only while the backend reports a running task | [requirements.md](../.kiro/specs/kantor-agen/requirements.md) |
| Design: `visual_state` is a pure backend function; Property 7 | [design.md](../.kiro/specs/kantor-agen/design.md) |
| Task 7.1 builds it, task 7.2 tests it | [tasks.md](../.kiro/specs/kantor-agen/tasks.md) |
| Code | [visual.py](../skeleton/office/visual.py) |
| Test | [test_prop_visual_state_truthful.py](../tests/properties/test_prop_visual_state_truthful.py) |

Req 2.8 and Property 7 were added in a spec revision (`2dbd236`) before any code existed. This fixed the rule "the browser never invents work" before the 3D code was written, and the test later checked it against the real heartbeat.

The spec and code drifted in two places, and I did not update the spec. The `Task` sketch in `design.md` says `cost: Decimal`, but the code uses integer rupiah (`cost_idr: int`, [models.py](../skeleton/runtime/models.py)), as the steering rule says. Property 7 says "working iff a task runs and the agent is not paused"; the code and test also require "no pending handoff" (handoff outranks running).

## 4. Steering

Three files in [.kiro/steering/](../.kiro/steering/): `product.md` (scope, MVP limits), `tech.md` (stack, layout), `rules.md` (nine hard rules). They have no inclusion front matter, so they use Kiro's default mode. Where the repository enforces the main rules:

- *Money is integer rupiah:* `*_idr INTEGER` columns; a property test rejects float money.
- *Policy checks live in backend code:* [engine.py](../skeleton/policy/engine.py); the Manager never asks the model to approve anything.
- *Customer messages stay in the outbox:* `CHECK (status = 'pending')` on the `outbox` table, tested.
- *The skeleton never imports from `apps/`:* `test_the_skeleton_still_knows_no_business_type` in [test_jasa_app.py](../tests/test_jasa_app.py) (Python files only).

I cannot show that each rule was loaded into each agent session. I can show that the code follows them.

## 5. Hooks

[run-tests-on-save.kiro.hook](../.kiro/hooks/run-tests-on-save.kiro.hook) fires on `fileEdited` for `skeleton/**/*.py` and `apps/**/*.py` and runs `.venv/bin/pytest -q`. It is enabled; the repository does not record how often it ran. The full suite takes about 192 s here, too slow for an on-save hook. A narrower command (only `tests/properties`, about 50 s) would have been better.

## 6. Property-based testing

Hypothesis, `max_examples=200` per `@given` test: 18 tests, up to 3,600 generated cases per run. 53 of the 1,196 pytest items are in `tests/properties/`.

| Property | Req. | Test file | What it protects |
|---|---|---|---|
| 1 Stock never negative | 6.1 | [test_prop_stock_never_negative.py](../tests/properties/test_prop_stock_never_negative.py) | random sales and receipts never leave a quantity below 0; the DB rejects a direct write too |
| 2 Sale total exact | 6.2 | [test_prop_sale_total_exact.py](../tests/properties/test_prop_sale_total_exact.py) | total equals the sum of qty × price − discount; invalid sales change nothing |
| 3 Daily spend ≤ limit | 5.1 | [test_prop_daily_spend_limit.py](../tests/properties/test_prop_daily_spend_limit.py) | allowed spend per day never exceeds the limit |
| 4 Blocked actions never run | 5.3 | [test_prop_blocked_actions.py](../tests/properties/test_prop_blocked_actions.py) | no ledger or outbox row for a blocked kind |
| 5 Messages never sent | 5.4 | [test_prop_customer_messages.py](../tests/properties/test_prop_customer_messages.py) | messages are queued as `pending`; no send function is called |
| 6 Paused means no new work | 3.4, 8.3 | [test_prop_paused_no_work.py](../tests/properties/test_prop_paused_no_work.py) | nothing starts while paused, also when the pause comes mid-run |
| 7 Visual state truthful | 2.8 | [test_prop_visual_state_truthful.py](../tests/properties/test_prop_visual_state_truthful.py) | WORKING only with a running task, also through the real heartbeat |

**Did PBT catch what unit tests missed?** I could not show that. No Hypothesis counterexample is recorded in the repository. To test the tests, I applied seven hand-written mutations to a throwaway copy of `4b96e5d` (not a mutation-testing tool, nothing committed). The property tests caught every one, and so did the other 1,141 Python tests (everything outside `tests/properties/`, without the two Node wrappers):

| Mutation | Property tests failing | Other tests failing |
|---|---|---|
| business pause check always false | 2 of 3 | 3 |
| agent pause check always false | 2 of 3 | 7 |
| daily-limit check removed | 2 of 2 | 68 |
| blocked-action check removed | 3 of 3 | 24 |
| duplicate sale lines not merged for the stock check | 2 of 32 | 2 |
| `visual_state` ignores pause | 4 of 6 | 25 |
| `visual_state` shows WORKING for a waiting agent | 4 of 6 | 5 |

So here the properties overlap with the unit tests. Their value is that each states a rule independent of one implementation.

## 7. Kiro features that were not used

- **MCP:** none. No `mcp.json` in the workspace or in the owner's `~/.kiro/settings`.
- **Custom agents:** none. No `.kiro/agents/`.
- **Kiro powers:** none installed (`~/.kiro/powers/installed.json` is empty).
- **Kiro CLI, Web, Crew:** nothing in the repository shows their use, so I claim none. `TODO(owner)`: confirm, and add any real use.

No task needed an external tool, so none was added for show.

## 8. What worked and what did not

- **Worked:** putting "truthful visual state" in the spec first. It became one pure function and one property.
- **Worked:** the code follows the steering rule (integer rupiah) rather than the stale `Decimal` sketch in the design.
- **Found late (Manager):** the outbox row for a customer message was written before the model ran, so it could never hold the drafted text. Fixed in `d87b4bf` by drafting first, then authorizing. The cost: a blocked message with no body still makes one model call (`test_rejected_customer_message_without_body_writes_no_outbox` in [test_manager.py](../tests/test_manager.py)).
- **Found late (sales feed):** with a short heartbeat the simulated feed replayed the same basket on every poll. Fixed in `4b96e5d`. I found no test that pins this fix.
- **Open design tension:** append-only and frozen-invoice triggers conflict with `ON DELETE` rules, so a half-failed Kantor Jasa onboarding cannot be cleaned up ([seed.py](../apps/kantor_jasa/seed.py), "Known limit"; [onboarding.py](../skeleton/office/onboarding.py)). Documented, not fixed.
- **Not verified:** the 30 fps and 5 s load targets (Req 2.10). [performance.md](performance.md) says they were never measured on real hardware.

## 9. Skeleton Crew evidence

The skeleton owns config, SQLite, clock and event bus (`core`), the policy engine and spend ledger (`policy`), heartbeat, Manager, pacing and LLM client (`runtime`), the activity log (`log`), and the API, SSE, onboarding and 3D front end (`office`). Each app adds roles, a data layer, seed data, a `BusinessTemplate` and an entry point. Lines at `4b96e5d`:

| Part | Lines |
|---|---|
| Skeleton Python (25 files) | 3,660 |
| Skeleton browser JS (18 files, without vendored three.js) + HTML/CSS | 6,298 + 992 |
| `apps/kantor_warung` Python (6 files) | 1,504 |
| `apps/kantor_jasa` Python (7 files, with its own schema) | 2,409 |
| Entry points `apps/kantor-warung/app.py`, `apps/kantor-jasa/app.py` | 19 each |
| Tests (Python / Node) | 12,721 / 2,117 |

The skeleton holds 10,950 of 14,901 product lines (73%). Against that 10,950, Warung adds 14% and Jasa 22%. Jasa is not small: its roles file alone has 1,112 lines. The folders `apps/kantor-warung` and `apps/kantor-jasa` are the two app folders with run entry points. A hyphen cannot be imported, so the code lives in `apps/kantor_warung` and `apps/kantor_jasa`. One test builds both apps on the same skeleton (`test_both_templates_can_live_in_one_registry_and_each_app_offers_only_its_own`). The browser preview mock in `ui-onboarding.js` does name both business types.

## 10. Reproduce

```
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q                    # 1196 tests, about 3 min (runs the Node suite too)
.venv/bin/pytest -q tests/properties   # 53 tests, about 1 min
node --test tests/js/                  # 112 tests
.venv/bin/python apps/kantor-warung/app.py --port 8000
.venv/bin/python apps/kantor-jasa/app.py --port 8001
git log --oneline kantor-agen/build
# one mutation, on a copy (expect failures):
REPO=$PWD; mkdir /tmp/mut && git archive HEAD | tar -x -C /tmp/mut
sed -i 's/if kind in blocked:/if False:/' /tmp/mut/skeleton/policy/engine.py
(cd /tmp/mut && PYTHONPATH=/tmp/mut $REPO/.venv/bin/python -m pytest -q -p no:cacheprovider tests/properties/test_prop_blocked_actions.py)
```
