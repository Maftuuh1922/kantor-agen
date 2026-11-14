# Kantor Agen: Product

Kantor Agen is a local web app for small businesses (UMKM). The owner opens a 3D office in the browser. AI employees (agents) watch the business data, decide what to do, and act inside limits the owner sets once. The owner reviews results and can pause agents at any time.

This is a Kiroween 2026 entry in the Skeleton Crew category. One shared skeleton powers two applications.

## The two apps

- **Kantor Warung** (`apps/kantor-warung`): for shops. Agents: cashier, stock, finance, marketing.
- **Kantor Jasa** (`apps/kantor-jasa`): for service businesses. Agents: booking, billing, follow-up.

A Manager Agent coordinates the others and picks the model for each task.

## Users

- **Owner**: a small business owner, not a developer. Sets a daily spending limit and a blocked-action list, reads the activity log, and pauses agents.
- **Judge or new user**: runs the demo with seed data, without entering a real business.

## MVP boundaries

- Customer messages are stored in the outbox and shown, never sent.
- No payments, bank integration, multiple accounts, native mobile app, or tax calculation.

Full details: `.kiro/specs/kantor-agen/`.
