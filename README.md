# Sverdrup

Measure and analyze home use of streaming services.

**Status:** The data pipeline (collector, processor, SQLite schema) is built and tested; the dashboard is next.

## Purpose

Sverdrup answers a simple question: is each streaming subscription worth paying for?

By analyzing DNS query logs and building a view of service usage over time, you can see:
- Time spent on each service per day
- Median days between uses per service
- Trends in subscription engagement

This data feeds retention decisions: which subscriptions are genuinely used, and which are just accumulating charges?

## Why the name

The sverdrup (Sv) is the oceanographers' unit of volume transport in ocean currents: 1 Sv = 10^6 m^3/s. It is named for the Norwegian oceanographer Harald Ulrik Sverdrup (1888-1957). This Sverdrup measures flow too: the flow of a household's streams.

## Architecture Overview

Sverdrup is a containerized system with three components:

1. **Collector** (Python) — Fetches DNS logs from the chosen profiles of one or more NextDNS accounts, backfills historical data on first run, then polls incrementally
2. **Processor** (Python) — Sessionizes DNS queries into viewing sessions and computes metrics
3. **Dashboard** (Go + htmx) — Lightweight web UI, mobile-friendly, shows time-per-service and usage frequency

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, including data, infrastructure, and security/trust layers.

## Quick Start

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed setup instructions.

**Minimal example (Docker Compose):**

```bash
# Create .env with your NextDNS accounts, keys and profiles
cp .env.example .env   # then edit

# Start the container
docker-compose up -d

# Access the dashboard at http://localhost:8080
```

## Supported Services

- Netflix
- Spotify
- Disney+
- YouTube
- Apple TV+
- HBO Max
- Paramount+
- Amazon Prime Video (with caveats; see ARCHITECTURE.md)

## Deployment

Designed for LAN-only deployment on home infrastructure, with an option to run behind a reverse proxy for broader access.

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed deployment guides, including Docker, manual setup, scheduler configuration, and reverse-proxy examples.

## Testing

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

The suite runs offline against a mock NextDNS API.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT — See [LICENSE](LICENSE)

## Future Work

- Local DNS server mode (eliminate NextDNS dependency)
- Per-profile usage tracking
- Advanced analytics and trend detection
- Mobile app (if needed beyond responsive web UI)
