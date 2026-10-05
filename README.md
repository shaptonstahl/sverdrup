# Sverdrup

Measure and analyze home use of streaming services.

**Status:** Architecture and design phase. Implementation to follow.

## Purpose

Sverdrup answers a simple question: is each streaming subscription worth paying for?

By analyzing DNS query logs and building a view of service usage over time, you can see:
- Time spent on each service per day
- Median days between uses per service
- Trends in subscription engagement

This data feeds retention decisions: which subscriptions are genuinely used, and which are just accumulating charges?

## Architecture Overview

Sverdrup is a containerized system with three components:

1. **Collector** (Python) — Fetches DNS logs from NextDNS, backfills historical data on first run, then polls incrementally
2. **Processor** (Python) — Sessionizes DNS queries into viewing sessions and computes metrics
3. **Dashboard** (Go + htmx) — Lightweight web UI, mobile-friendly, shows time-per-service and usage frequency

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## Quick Start

[To be added during implementation phase]

## Deployment

Designed for LAN-only deployment on home infrastructure, with an option to run behind a reverse proxy for broader access.

See [DEPLOYMENT.md](DEPLOYMENT.md) for details.

## License

MIT
