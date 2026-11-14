# Design Document

## Overview

Kantor Agen runs as a local web app: a Python backend (FastAPI) and a browser front end (Three.js). The backend hosts a shared skeleton. Two applications, Kantor Warung and Kantor Jasa, plug into the skeleton with their own roles, data models, and triggers.

The core idea is that the backend, not the browser, decides everything. A heartbeat loop evaluates each agent's triggers. A Manager Agent routes the resulting tasks. A policy engine approves or rejects every spending or outbound action before it runs. The browser only renders state and sends owner controls.

Mapping to requirements: onboarding (Req 1) and the 3D office (Req 2) live in the skeleton's office module. Autonomy (Req 3) is the heartbeat. Coordination and model choice (Req 4) are in the Manager. Guardrails (Req 5) and stock integrity (Req 6) are in the policy engine and the app's data layer. The activity log (Req 7), owner control (Req 8), the two-app layout (Req 9), demo and setup (Req 10), and tests (Req 11) are covered in the sections below.

## Architecture

```
Browser (Three.js office)
   │  HTTP (REST for commands) + SSE (state stream)
   ▼
FastAPI server (skeleton/office)
   │
   ├── Heartbeat loop ──► Manager Agent ──► Agents (app-supplied roles and triggers)
   │                            │
   │                            ├── LLM client (OpenAI-compatible, only for text and judgment)
   │                            └── Policy engine (checks every spend and outbound action)
   │
   └── SQLite (business data, policy, spend ledger, activity log, outbox)
```

Single process, single SQLite file per application. No external queue, so the MVP runs on one laptop with one command.

## Repository Layout

```
kantor-agen/
├── skeleton/
│   ├── core/          config, database access, event bus, clock
│   ├── policy/        policy engine and spend ledger
│   ├── runtime/       heartbeat, manager, agent base class, LLM client
│   ├── log/           activity log and daily summary
│   └── office/        FastAPI app factory, SSE stream, static front end
│       └── static/    index.html, office.js (Three.js scene), vendored three.module.js
│           └── assets/ characters (glTF), animations (glTF), furniture (glTF)
├── apps/
│   ├── kantor-warung/ roles, triggers, models, seed data, app.py entry point
│   └── kantor-jasa/   roles, triggers, models, seed data, app.py entry point
├── tests/             property-based and unit tests (Hypothesis, pytest)
├── .kiro/             steering, specs, hooks
├── CREDITS.md         source and license of every 3D asset
└── README.md
```

Each application imports only from `skeleton/`. The skeleton never imports from an application. This dependency direction is what makes the skeleton reusable (Req 9).

## Components and Interfaces

### Agent and Role

An application defines roles. A role has a name, a desk index, a set of triggers, and the actions it may propose.

```python
@dataclass
class Task:
    agent_id: int
    kind: str            # "read" | "draft" | "spend" | "customer_message"
    payload: dict
    cost: Decimal        # 0 for non-spending tasks
    reason: str          # shown to the owner in the activity log

class Role(Protocol):
    name: str
    def triggers(self, ctx: BusinessContext) -> list[Task]: ...
    def run(self, task: Task, ctx: BusinessContext) -> ActionResult: ...
```

`triggers` is pure rule logic that reads the database. It returns tasks only when a condition holds (for example, stock below minimum). No model call happens here (Req 3.3).

### Heartbeat

- Runs as an asyncio background task at a configurable interval (default 300 seconds).
- For each business that is not paused, it calls every active agent's `triggers`, then hands the tasks to the Manager.
- Holds one asyncio lock per business. Only one task runs at a time for that business, which removes write races between agents (Req 4.2).
- Before each task step, it checks the pause flags. A running task finishes its current step, then stops (Req 8.3).

### Manager Agent

- Takes tasks, assigns handoffs to the right agent, and records each handoff in the activity log (Req 4.1).
- Chooses the model for a task by task kind. `read` and `spend` never call a model. `draft` uses the low-cost model. `customer_message` drafts text with the stronger model. Model names come from config, not code.
- On an LLM error, it marks the task as rule-only, runs the rule path if one exists, and logs the failure (Req 4.4).

### Policy Engine

The only gate for money and outbound actions. Every spending task passes through `authorize`.

```python
def authorize(business_id, task) -> Decision:
    # executed inside one SQLite transaction (BEGIN IMMEDIATE)
    if task.kind not in ALLOWED_KINDS or task.kind in policy.blocked_actions:
        return Decision.reject("blocked action type")
    spent = ledger.sum_today(business_id)
    if spent + task.cost > policy.daily_limit:
        return Decision.reject("daily spending limit reached")
    ledger.insert(business_id, task.cost, task.agent_id, now())
    return Decision.allow()
```

The policy check reads and writes in the same transaction, so two concurrent spend requests cannot both pass the limit (Req 5.1, 5.5). `customer_message` tasks never reach a send function. They are written to the outbox table with status `pending` (Req 5.4).

### Office Module

- REST endpoints: `GET /api/office` (agents and desks), `GET /api/agents/{id}` (panel data), `POST /api/office/pause`, `POST /api/agents/{id}/pause`, `PUT /api/policy`.
- SSE endpoint `GET /api/events` streams activity entries and agent state changes to the browser.

### Visual State (backend)

The backend, not the browser, decides what each character should be doing. This keeps the scene truthful (Req 2.8).

```python
class VisualState(str, Enum):
    WORKING = "working"        # task running: sit at desk, type
    HANDOFF = "handoff"        # walk to target agent's desk, talk
    WAITING = "waiting"        # stand at desk, raised hand, amber marker
    IDLE = "idle"              # browser picks an idle behavior
    PAUSED = "paused"          # stand still at desk, grey marker

def visual_state(agent, running_task, pending_handoff) -> VisualState: ...
```

`visual_state` is a pure function of backend state. Every change is pushed over SSE as `{agent_id, state, target_agent_id, bubble_text}`.

### Character Rendering (browser)

- **Assets:** rigged human characters from Quaternius Universal Base Characters and clips from Quaternius Universal Animation Library. Both use one humanoid rig and are published under CC0 (source: https://quaternius.com/packs/universalanimationlibrary.html). Furniture from CC0 packs. Every asset is listed in `CREDITS.md` (Req 2.11).
- **Distinct looks:** each agent gets a different base character and outfit color from a fixed palette, seeded by agent id, so looks stay the same across reloads (Req 2.2).
- **Animation:** one `THREE.AnimationMixer` per character. State changes call `crossFadeTo` over 0.3 seconds (Req 2.3). Clip map: idle, walk, sit-down, sit-idle, type, talk, wave, stretch, drink.
- **Typing fallback:** if the library has no typing clip, the sit-idle clip plays and the forearm bones get a small procedural oscillation.

### Movement and Pathfinding (browser)

- The floor is a 0.5 m grid. Desks, walls, and furniture mark cells as blocked.
- A* finds the path between two cells. The path is smoothed by removing points that have a clear line of sight to each other.
- Characters walk at 1.2 m/s and turn toward the next waypoint with a short rotation ease (Req 2.4).
- Two characters do not reserve the same target cell. A character that arrives at an occupied cell waits for 1 second and picks the nearest free neighbor.

### Behavior Controller (browser)

A small state machine per character turns backend `VisualState` into motion:

| Backend state | Behavior |
|---|---|
| `working` | walk to own desk if away, sit down, type |
| `handoff` | walk to the target agent's desk, stand, talk for 3 to 5 seconds, walk back |
| `waiting` | walk to own desk, stand, wave once, then idle with amber marker |
| `idle` | every 20 to 60 seconds, pick one: stretch at desk, get a drink at the pantry, chat with another idle agent |
| `paused` | stand still at own desk with a grey marker |

A new backend state always interrupts an idle behavior. Idle behaviors never show the type animation, so idle is never mistaken for work (Req 2.7, 2.8).

### Performance

- Characters share geometry and animation clips. Only materials differ per agent.
- Shadow map 1024 px from one directional light. Low-quality mode turns off shadows and uses lower-detail character meshes (Req 2.10).
- Assets load in parallel with a progress bar. The scene starts after the furniture and the first character are ready, and other characters fade in as they load.

## Data Models

SQLite tables. Money is stored as integer rupiah to avoid floating-point error.

| Table | Key columns |
|---|---|
| `business` | id, name, type, paused |
| `agent` | id, business_id, role, name, desk_index, paused |
| `policy` | business_id, daily_limit_idr, blocked_actions (JSON array) |
| `spend_ledger` | id, business_id, agent_id, amount_idr, day, created_at |
| `product` | id, business_id, name, qty, min_qty, unit_price_idr, unit_cost_idr |
| `sale` | id, business_id, created_at, total_idr |
| `sale_item` | sale_id, product_id, qty, unit_price_idr, discount_idr |
| `purchase_order` | id, product_id, qty, cost_idr, status (`placed` or `pending`) |
| `activity` | id, business_id, agent_id, action, reason, cost_idr, status, created_at |
| `outbox` | id, business_id, agent_id, channel, recipient, body, status (`pending` only in MVP) |

Stock changes happen only through `record_sale` and `receive_purchase`. Both functions update `product.qty` in the same transaction as the sale or purchase row.

## Correctness Properties

These properties are the targets of the property-based tests (Req 11.1). Each one is checked with random sequences of operations.

### Property 1: Stock never negative

**Validates: Requirements 6.1**

For any sequence of sales and purchase receipts, every `product.qty` is greater than or equal to 0. A sale larger than the stock is rejected, not applied.

### Property 2: Sale total is exact

**Validates: Requirements 6.2**

For any set of items, `sale.total_idr` equals the sum of `qty * unit_price_idr - discount_idr` over its items, and each product's stock drops by exactly the sold quantity.

### Property 3: Daily spend never exceeds the limit

**Validates: Requirements 5.1**

For any sequence of spend requests within a day, the sum of allowed amounts is at most `daily_limit_idr`.

### Property 4: Blocked actions never execute

**Validates: Requirements 5.3**

For any action type in `blocked_actions`, `authorize` returns reject and no ledger row is written.

### Property 5: Customer messages are never sent

**Validates: Requirements 5.4**

For any task of kind `customer_message`, the outcome is an outbox row with status `pending` and no call to a send function.

### Property 6: Paused means no new work

**Validates: Requirements 3.4, 8.3**

For any state where the business or the agent is paused, the heartbeat starts zero new tasks.

### Property 7: Visual state matches real state

**Validates: Requirements 2.8**

For any combination of agent pause flag, running task, and pending handoff, `visual_state` returns `working` if and only if the agent has a running task and is not paused.

## Error Handling

| Failure | Response |
|---|---|
| LLM API error or timeout | Use rule-only path if it exists. Otherwise skip the task. Log the error (Req 4.4). |
| Policy rejection | Do not execute. Log the reason. Notify the owner when the daily limit is reached (Req 5.2). |
| SQLite busy | Retry the transaction up to 3 times with a short backoff, then log and skip. |
| Heartbeat task raises an exception | Catch per agent, log it, and continue with the next agent. The loop does not stop. |
| Invalid owner input (negative price, unknown product) | Reject at the API layer with a validation error. Nothing is written. |

## Configuration and Secrets

- `skeleton/core/config.py` loads settings from environment variables and an optional `.env` in the server working directory.
- Required: `KANTOR_LLM_BASE_URL`, `KANTOR_LLM_API_KEY`, `KANTOR_MODEL_LOW`, `KANTOR_MODEL_STRONG`.
- The API key is never returned by any endpoint and never sent to the browser (Req 10.2).
- Heartbeat interval and default daily limit are configurable (default daily limit Rp 100.000, as proposed in the requirements).

## Testing Strategy

- **Property-based tests** (Hypothesis) for the seven properties above, under `tests/properties/`. Each test runs at least 200 generated cases.
- **Front-end checks:** a unit test for the A* grid (no path crosses a blocked cell) and a manual frame-rate check with 8 agents in normal and low-quality mode.
- **Unit tests** (pytest) for the policy engine's reject paths, the Manager's model routing, and the office REST endpoints.
- **Heartbeat tests** with a fake clock and a fake LLM client, so tests are deterministic and need no network.
- **Smoke test** per application: start the server with seed data, run one heartbeat cycle, and check that at least one agent produced an activity row.
- The LLM client is replaced by a fake in all tests. No test calls a real model API.

## Open Decisions

- Three.js is vendored into `skeleton/office/static/vendor/` so the demo runs offline. The alternative is a CDN import, which is simpler but needs internet.
- SSE is used instead of WebSocket because the data flow is one-way (server to browser). This keeps the code smaller.
- The free tier of the Quaternius packs may not include every clip in the clip map (for example typing). The typing fallback above covers this. The clip list will be confirmed after download.
