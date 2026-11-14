# Implementation Plan

## Overview

This plan builds Kantor Agen in four blocks: the Python skeleton (policy, runtime, activity log), the lifelike 3D office, the two applications (Kantor Warung and Kantor Jasa), and the submission material. Property-based tests for the seven correctness properties are written next to the code they check. Tasks marked with `*` are tests and can be skipped for a faster MVP, but the guardrail properties (3, 4, 5) should be kept because they back the main claim of the project.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "id": 0,
      "tasks": [
        "1.1"
      ]
    },
    {
      "id": 1,
      "tasks": [
        "1.2",
        "2.1",
        "2.2",
        "2.3"
      ]
    },
    {
      "id": 2,
      "tasks": [
        "3.1",
        "4.1",
        "5.1",
        "5.2"
      ]
    },
    {
      "id": 3,
      "tasks": [
        "3.2",
        "3.3",
        "3.4",
        "3.5",
        "5.3"
      ]
    },
    {
      "id": 4,
      "tasks": [
        "5.4",
        "8.1",
        "8.2"
      ]
    },
    {
      "id": 5,
      "tasks": [
        "5.5",
        "5.6",
        "7.1",
        "8.3",
        "8.4"
      ]
    },
    {
      "id": 6,
      "tasks": [
        "6",
        "7.2",
        "7.3",
        "8.5",
        "8.6"
      ]
    },
    {
      "id": 7,
      "tasks": [
        "8.7",
        "10.1",
        "8.9"
      ]
    },
    {
      "id": 8,
      "tasks": [
        "8.8"
      ]
    },
    {
      "id": 9,
      "tasks": [
        "9",
        "10.2",
        "10.3",
        "10.4",
        "11.1"
      ]
    },
    {
      "id": 10,
      "tasks": [
        "10.5",
        "10.6",
        "11.2"
      ]
    },
    {
      "id": 11,
      "tasks": [
        "11.3",
        "11.4",
        "11.5"
      ]
    },
    {
      "id": 12,
      "tasks": [
        "12"
      ]
    },
    {
      "id": 13,
      "tasks": [
        "13.1",
        "13.2",
        "13.3"
      ]
    },
    {
      "id": 14,
      "tasks": [
        "13.4"
      ]
    }
  ]
}
```

## Tasks

- [x] 1. Project setup and Kiro workspace
  - [x] 1.1 Create the Python project and folder layout
    - Create `skeleton/` (core, policy, runtime, log, office), `apps/kantor-warung/`, `apps/kantor-jasa/`, and `tests/`
    - Add `pyproject.toml` with FastAPI, uvicorn, httpx, pytest, and hypothesis
    - Add `.env.example` with `KANTOR_LLM_BASE_URL`, `KANTOR_LLM_API_KEY`, `KANTOR_MODEL_LOW`, `KANTOR_MODEL_STRONG`, and ensure `.env` is in `.gitignore` while `.kiro/` is not
    - _Requirements: 9.1, 10.2, 11.2_
  - [x] 1.2 Add Kiro steering files and a hook
    - Write `.kiro/steering/product.md`, `tech.md`, and `rules.md` (rupiah as integer, policy checks only in backend code, no real customer messages in the MVP)
    - Add a hook that runs the test suite when a Python file under `skeleton/` or `apps/` is saved
    - _Requirements: 11.2_

- [x] 2. Skeleton core
  - [x] 2.1 Implement config loading
    - Load settings from environment variables and an optional `.env`
    - Default daily limit Rp 100.000, default heartbeat 300 seconds
    - Never expose the API key through any endpoint
    - _Requirements: 3.1, 10.2_
  - [x] 2.2 Implement the SQLite layer and schema
    - Create all tables from the design with money as integer rupiah
    - Provide a transaction helper with `BEGIN IMMEDIATE` and a retry of up to 3 times on busy
    - _Requirements: 5.5, 6.1_
  - [x] 2.3 Implement the event bus and clock
    - Add an injectable clock so tests control time
    - Add an in-process event bus used by the SSE stream
    - _Requirements: 3.1, 7.1_

- [x] 3. Policy engine and spend ledger
  - [x] 3.1 Implement `authorize` with ledger insert in one transaction
    - Reject blocked action types, reject spending above the daily limit, and write the ledger row only on allow
    - Route `customer_message` tasks to the outbox with status `pending` and never call a send function
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_
  - [x]* 3.2 Write property test for the daily spending limit
    - **Property 3: Daily spend never exceeds the limit**
    - **Validates: Requirements 5.1**
  - [x]* 3.3 Write property test for blocked actions
    - **Property 4: Blocked actions never execute**
    - **Validates: Requirements 5.3**
  - [x]* 3.4 Write property test for customer messages
    - **Property 5: Customer messages are never sent**
    - **Validates: Requirements 5.4**
  - [x]* 3.5 Write unit tests for the reject paths
    - Cover limit reached, blocked type, invalid cost, and two concurrent spend requests
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 4. Activity log
  - [x] 4.1 Implement the append-only activity log and daily summary
    - Record agent, time, action, reason, cost, and status for every action
    - Build the daily summary of actions, spending, and pending items
    - _Requirements: 7.1, 7.2, 7.3_

- [x] 5. Agent runtime
  - [x] 5.1 Implement the Role protocol and Task model
    - Define `Task` and `Role` as in the design, with `triggers` as pure rule logic that makes no model call
    - _Requirements: 3.2, 3.3_
  - [x] 5.2 Implement the LLM client
    - Use an OpenAI-compatible `/chat/completions` client with timeout and error handling
    - Provide a fake client for tests and demo mode
    - _Requirements: 4.3, 4.4, 10.2_
  - [x] 5.3 Implement the Manager Agent
    - Assign handoffs and record them in the activity log
    - Pick the model by task kind, and fall back to the rule-only path when the model fails
    - _Requirements: 4.1, 4.2, 4.3, 4.4_
  - [x] 5.4 Implement the heartbeat loop
    - Evaluate each active agent's triggers on a schedule with one lock per business
    - Check pause flags before every step, and catch exceptions per agent so the loop continues
    - _Requirements: 3.1, 3.2, 3.4, 8.1, 8.3_
  - [x]* 5.5 Write property test for pause behavior
    - **Property 6: Paused means no new work**
    - **Validates: Requirements 3.4, 8.3**
  - [x]* 5.6 Write unit tests for the Manager and heartbeat
    - Use a fake clock and fake LLM; cover model routing, LLM failure, and handoff order
    - _Requirements: 4.1, 4.2, 4.4_

- [x] 6. Checkpoint: skeleton backend
  - Run the full test suite and ask the owner if anything needs to change before the front end work

- [x] 7. Visual state and office API
  - [x] 7.1 Implement `visual_state` and the SSE stream
    - Return working, handoff, waiting, idle, or paused from backend state only
    - Push `{agent_id, state, target_agent_id, bubble_text}` over `GET /api/events`
    - _Requirements: 2.8_
  - [x]* 7.2 Write property test for visual state
    - **Property 7: Visual state matches real state**
    - **Validates: Requirements 2.8**
  - [x] 7.3 Implement the office REST endpoints
    - `GET /api/office`, `GET /api/agents/{id}`, `POST /api/office/pause`, `POST /api/agents/{id}/pause`, `PUT /api/policy`
    - Validate input at the API layer and reject invalid values without writing
    - _Requirements: 1.3, 1.4, 2.9, 8.1, 8.2_

- [x] 8. 3D office front end
  - [x] 8.1 Build the Three.js scene
    - Vendor `three.module.js`, build the room with desks, pantry, meeting table, lights, and one shadow-casting directional light
    - Add the low-quality mode toggle
    - _Requirements: 2.1, 2.10_
  - [x] 8.2 Add CC0 assets and credits
    - Download Quaternius characters, animation clips, and furniture, then list each source and license in `CREDITS.md`
    - Confirm which clips exist, and implement the typing fallback if there is no typing clip
    - _Requirements: 2.2, 2.11_
  - [x] 8.3 Implement character loading and animation
    - Load rigged characters with one `AnimationMixer` each, give each agent a distinct look seeded by agent id, and crossfade clips over 0.3 seconds
    - _Requirements: 2.2, 2.3_
  - [x] 8.4 Implement grid pathfinding and walking
    - Build the 0.5 m grid, A* with path smoothing, walking at 1.2 m/s with turn easing, and the occupied-cell wait rule
    - _Requirements: 2.4_
  - [x]* 8.5 Write a unit test for the A* grid
    - No path may cross a blocked cell, and unreachable targets return no path
    - _Requirements: 2.4_
  - [x] 8.6 Implement the behavior controller
    - Map backend states to behaviors: working, handoff, waiting, idle, and paused, with idle behaviors that never use the typing animation
    - _Requirements: 2.5, 2.6, 2.7, 2.8_
  - [x] 8.7 Implement the agent panel, speech bubbles, and status markers
    - Show role, status, recent actions, spending today, and the latest action in a bubble
    - _Requirements: 2.6, 2.9_
  - [x] 8.8 Measure and tune performance
    - Check load time and frame rate with 8 agents in normal and low-quality mode
    - _Requirements: 2.10_
  - [x] 8.9 Diversify character looks
    - Mix male and female bodies, vary height, build, hair, and clothing so the staff do not all look like the same muscular man, and keep each agent's look the same on every reload
    - _Requirements: 2.2_

- [x] 9. Checkpoint: skeleton demo
  - Run the skeleton with a dummy role and confirm that an agent works, hands off, waits, and idles in the 3D office

- [x] 10. Kantor Warung application
  - [x] 10.1 Implement the data layer for products, sales, and purchase orders
    - `record_sale` and `receive_purchase` update stock in the same transaction as the sale or purchase row
    - Reject a sale larger than the stock without applying it
    - _Requirements: 6.1, 6.2_
  - [x]* 10.2 Write property test for non-negative stock
    - **Property 1: Stock never negative**
    - **Validates: Requirements 6.1**
  - [x]* 10.3 Write property test for exact sale totals
    - **Property 2: Sale total is exact**
    - **Validates: Requirements 6.2**
  - [x] 10.4 Implement the cashier, stock, finance, and marketing roles
    - Stock agent orders when stock is under the minimum and within the limit, or records the order as pending
    - Marketing agent drafts a promo after sales fall for three days in a row
    - Finance agent prepares the daily summary
    - _Requirements: 1.2, 3.2, 6.3_
  - [x] 10.5 Add onboarding, seed data, and the app entry point
    - Ask for business type and name, force the policy setup before any spending, and load demo data
    - _Requirements: 1.1, 1.2, 1.3, 10.1, 10.3_
  - [x]* 10.6 Write a smoke test
    - Start with seed data, run one heartbeat, and check that at least one agent wrote an activity row
    - _Requirements: 3.2, 10.1_

- [x] 11. Kantor Jasa application
  - [x] 11.1 Implement the data layer for bookings, invoices, and customers
    - An invoice must not change after the owner or policy approves it
    - _Requirements: 5.1, 6.2_
  - [x] 11.2 Implement the booking, billing, and follow-up roles
    - Booking agent confirms slots, billing agent drafts invoices, follow-up agent drafts reminders into the outbox
    - _Requirements: 1.2, 3.2, 5.4_
  - [x] 11.3 Add onboarding, seed data, and the app entry point
    - Reuse the skeleton onboarding with Kantor Jasa roles and demo data
    - _Requirements: 1.1, 1.2, 9.2, 10.1, 10.3_
  - [-]* 11.4 Write a smoke test
    - Start with seed data, run one heartbeat, and check that at least one agent wrote an activity row
    - _Requirements: 3.2, 10.1_
  - [x] 11.5 Pace agent work so staff are visibly working
    - Give each task a short working time while the backend reports it as running
    - Pacing is on by default in the live apps, off in tests, and documented honestly
    - _Requirements: 2.5, 2.8, 3.2_

- [-] 12. Checkpoint: both applications
  - Run the full test suite and run both applications end to end; ask the owner if anything needs to change

- [ ] 13. Documentation and submission material
  - [ ] 13.1 Write the README
    - Explain the idea, the skeleton-plus-two-apps layout, one-command run per application, and the demo mode with the fake LLM
    - _Requirements: 9.3, 10.3_
  - [ ] 13.2 Write the Kiro usage write-up
    - Describe how specs, steering, hooks, and property-based tests shaped the build, with real examples from this repository
    - _Requirements: 11.2_
  - [ ] 13.3 Check repository readiness for Devpost
    - Public repo, visible MIT license, `.kiro/` committed, two app folders, `CREDITS.md`, and no secrets in the history
    - _Requirements: 9.1, 10.2, 11.2_
  - [ ] 13.4 Record the demo video
    - Show the working app on the device it was built for, under three minutes, uploaded as public to YouTube
    - _Requirements: 10.1_

## Notes

- The demo runs with a fake LLM client by default, so it needs no API key, no credits, and no internet.
- Quaternius assets are CC0. Confirm the clip list (especially typing) in task 8.2 before building the behavior controller.
- Time budget: the submission deadline is 4 December 2026, 14:00 Pacific Time. Task 13.4 (demo video) needs the owner's PC and must not be left to the last day.
- Never commit `.env` or any API key. The `.kiro/` directory must stay committed.
