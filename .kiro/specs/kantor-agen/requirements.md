# Requirements Document

## Introduction

Kantor Agen is a web app for small businesses (UMKM). The owner opens a 3D office in the browser. AI employees (agents) work on their own: they watch the business data, decide what to do, and act inside limits the owner sets once. The owner reviews results and can pause agents at any time.

This project is a Kiroween 2026 entry in the Skeleton Crew category. One shared skeleton powers two applications: Kantor Warung and Kantor Jasa.

Status: requirements draft v0.1, pending owner review.

## Glossary

- **Owner**: the UMKM owner who uses the app.
- **Agent**: an autonomous AI worker with a role (cashier, stock, finance, marketing, booking, billing, follow-up) and a desk in the office.
- **Manager Agent**: the agent that coordinates the other agents and picks the model for each task.
- **Heartbeat**: the scheduler tick that checks agent triggers without an owner command.
- **Policy**: the limits the owner sets: a daily spending limit and a list of blocked action types.
- **Guardrail**: a limit enforced in backend code, not only in a model prompt.
- **Outbox**: the queue of customer messages. In the MVP, messages are stored and shown, never sent.
- **Skeleton**: the shared core (office scene, agent runtime, policy engine, heartbeat, activity log, storage).

## Requirements

### Requirement 1: Business onboarding

**User Story:** As an owner, I want to pick my business type once, so the office is set up with the right AI staff.

#### Acceptance Criteria

1. WHEN a new owner opens the app for the first time, THE app SHALL ask for the business type and business name.
2. WHEN the owner picks a business type, THE app SHALL create the matching agents and place each one at a desk. Kantor Warung creates cashier, stock, finance, and marketing agents. Kantor Jasa creates booking, billing, and follow-up agents.
3. WHEN onboarding finishes, THE app SHALL require the owner to set the policy (daily spending limit and blocked actions) before any agent can spend money.
4. THE app SHALL let the owner rename agents and change their roles later.

### Requirement 2: 3D office view

**User Story:** As an owner, I want to see my AI staff in a 3D office, so I can understand what they are doing at a glance.

#### Acceptance Criteria

1. THE app SHALL render the office in Three.js with one desk per agent, using simple geometric models that load within 3 seconds on a typical laptop.
2. WHEN an agent is working, THE app SHALL show the agent moving to its desk and display its latest action in a speech bubble.
3. WHEN an agent is waiting for the owner, THE app SHALL show a visible status marker on that agent's desk.
4. WHEN the owner clicks an agent, THE app SHALL open a panel with the agent's role, status, recent actions, and spending today.

### Requirement 3: Autonomous operation

**User Story:** As an owner, I want my agents to act on their own, so I do not have to tell them what to do.

#### Acceptance Criteria

1. WHILE the office is running, THE heartbeat SHALL evaluate every agent's triggers on a fixed schedule (default every 5 minutes, configurable).
2. WHEN a trigger condition is met, THE agent SHALL start its task without an owner command.
3. WHEN a trigger is evaluated, THE app SHALL decide with rules first and call the model only when the task needs generated text or judgment, so routine checks use no model tokens.
4. IF the owner has paused the office, THEN THE heartbeat SHALL NOT start new agent tasks.

### Requirement 4: Manager Agent coordination

**User Story:** As an owner, I want one manager agent to coordinate the team, so agents do not conflict with each other.

#### Acceptance Criteria

1. WHEN an agent raises a task that belongs to another agent, THE Manager Agent SHALL assign it and record the handoff in the activity log.
2. WHEN two agents try to change the same record at the same time, THE Manager Agent SHALL run the changes in order and reject the later change if it conflicts with the earlier one.
3. THE Manager Agent SHALL choose the model for each task by task type, using a low-cost model for summaries and a stronger model for decisions.
4. IF the model API fails, THEN THE Manager Agent SHALL continue with rule-based actions only and record the failure in the activity log.

### Requirement 5: Guardrails enforced in code

**User Story:** As an owner, I want firm limits on what my agents can do, so an AI mistake cannot cost more than I allowed.

#### Acceptance Criteria

1. WHEN an agent proposes a spending action, THE policy engine SHALL check it against the daily spending limit before execution, and SHALL reject any action that would exceed the limit.
2. WHEN the daily spending limit is reached, THE policy engine SHALL block further spending for the rest of the day and notify the owner.
3. THE policy engine SHALL block every action type on the owner's blocked list, regardless of what the model proposes.
4. WHEN an agent proposes a customer-facing message, THE app SHALL place the message in the outbox and SHALL NOT send it in the MVP.
5. THE policy check SHALL run in backend code and SHALL NOT depend on the model prompt, so a model output alone cannot bypass the policy.

### Requirement 6: Stock and money integrity

**User Story:** As an owner, I want my stock and sales records to stay correct, so the agents make decisions from true numbers.

#### Acceptance Criteria

1. THE stock records SHALL NEVER show a negative quantity for any product.
2. WHEN a sale is recorded, THE app SHALL reduce the stock by the sold quantity, and THE sale total SHALL equal the sum of item prices minus discounts.
3. WHEN the stock of a product falls below its minimum, THE stock agent SHALL place a purchase order within the daily spending limit, or record it as pending if the limit is already reached.

### Requirement 7: Activity log and reports

**User Story:** As an owner, I want a clear record of what each agent did and why, so I can trust the system.

#### Acceptance Criteria

1. WHEN an agent performs any action, THE app SHALL record the agent, time, action, reason, and cost in the activity log.
2. THE app SHALL send the owner a daily summary of actions, spending, and pending items.
3. THE activity log SHALL be append-only in the UI, with no edit or delete option.

### Requirement 8: Owner control

**User Story:** As an owner, I want to stop or change my agents at any time.

#### Acceptance Criteria

1. THE app SHALL provide one pause control for the whole office and one pause control for each agent.
2. WHEN the owner changes the policy, THE app SHALL apply the new limits to the next action.
3. WHEN the owner pauses the office, THE app SHALL stop new tasks at once, and each running task SHALL finish its current step and then stop.

### Requirement 9: Two applications on one skeleton

**User Story:** As a developer, I want the two business apps to share one core, so the skeleton is proven reusable.

#### Acceptance Criteria

1. THE repository SHALL contain two separate application folders, `apps/kantor-warung` and `apps/kantor-jasa`, and both SHALL import the shared `skeleton/` core.
2. THE skeleton SHALL own the office scene, agent runtime, policy engine, heartbeat, and activity log. Each application SHALL supply only its roles, data models, and triggers.
3. THE repository README SHALL explain how to run each application.

### Requirement 10: Demo and setup

**User Story:** As a judge or new user, I want to run a demo with sample data, so I can see the agents work without entering a real business.

#### Acceptance Criteria

1. THE app SHALL include seed data for each business type, so the demo runs without manual data entry.
2. THE app SHALL read the model API key only from the server environment (`.env`) and SHALL NOT expose it to the browser or the repository.
3. THE app SHALL run locally with one command per application, documented in the README.

### Requirement 11: Testing and Kiro usage

**User Story:** As a developer, I want the guardrails and business rules tested with property-based tests, so correctness does not depend only on hand-picked examples.

#### Acceptance Criteria

1. THE test suite SHALL include property-based tests (Hypothesis) for three properties: stock is never negative, daily spending never exceeds the limit, and blocked actions never execute.
2. THE repository SHALL contain `.kiro/` at its root with steering files, this spec, and at least one hook, and SHALL NOT list `.kiro/` in `.gitignore`.

## Out of Scope (MVP)

- Real payment processing and bank integration.
- Sending WhatsApp, email, or any other real customer message. Messages go to the outbox only.
- Multiple owner accounts, billing, and subscriptions.
- Native mobile app.
- Tax calculation.
