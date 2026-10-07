# Deployment Guide

## Quick Start

### Prerequisites
- Docker and Docker Compose
- NextDNS account with API access enabled
- For each NextDNS account: its API key, and the IDs of the profiles you want collected

### Environment Setup

All configuration is environment variables in one `.env` file (git-ignored; never commit it). Start from the example:

```bash
cp .env.example .env
```

```
# Short names for your NextDNS accounts (letters, digits, underscores)
SVERDRUP_ACCOUNTS=home,family

# Per account NAME, uppercased: the account's API key and the profiles to collect.
# Profiles the account owns but you leave out are not collected.
NEXTDNS_HOME_API_KEY=your_home_account_api_key
NEXTDNS_HOME_PROFILES=abc123,def456
NEXTDNS_FAMILY_API_KEY=your_family_account_api_key
NEXTDNS_FAMILY_PROFILES=fed321

# Optional (defaults shown)
NEXTDNS_API_URL=https://api.nextdns.io
COLLECTOR_INTERVAL=15
PROCESSOR_INTERVAL=60
SESSION_GAP_MINUTES=15
TZ=UTC
DASHBOARD_PORT=8080
```

`TZ` is an IANA zone such as `America/New_York`; it decides where one day's metrics end. `COLLECTOR_INTERVAL` and `PROCESSOR_INTERVAL` are minutes that cron can repeat evenly: under 60 they must divide 60 (for example 5, 15 or 30); from 60 up they must be whole hours that divide 24 (60, 120, 180, 240, 360, 480, 720 or 1440). The container exits at startup, naming the variable, for any other value. The collector checks this configuration at startup and names every variable that is missing or malformed. The older `NEXTDNS_API_KEY` and `NEXTDNS_PROFILE_ID` variables are no longer read; move them into an account.

### Run with Docker Compose (Miracle / Synology layout)

The repository ships `docker-compose.example.yml`, not a `docker-compose.yml`. A deployment keeps its own `docker-compose.yml`, `.env` and `data/` in a parent project directory, with the git clone in a `srv/` subfolder beneath it. A `git pull` then never touches your configuration or your database. On Miracle the project directory is `/volume1/docker/sverdrup`:

```
/volume1/docker/sverdrup/
  docker-compose.yml   # copied from srv/docker-compose.example.yml
  .env                 # copied from srv/.env.example
  data/                # SQLite database, bind-mounted at /data
  srv/                 # git clone of sverdrup
```

1. Clone the repository into the `srv` subfolder:
   ```bash
   mkdir -p /volume1/docker/sverdrup
   cd /volume1/docker/sverdrup
   git clone https://github.com/shaptonstahl/sverdrup.git srv
   ```
2. Copy the Compose example up into the project directory:
   ```bash
   cp srv/docker-compose.example.yml docker-compose.yml
   ```
3. Copy the environment example and fill in your NextDNS accounts, API keys, profile IDs and `TZ` (see [Environment Setup](#environment-setup)):
   ```bash
   cp srv/.env.example .env
   chmod 600 .env
   ```
4. Create the data directory outside the repo:
   ```bash
   mkdir -p /volume1/docker/sverdrup/data
   ```
5. Build and start the service from the project directory:
   ```bash
   docker compose up -d
   ```

Compose reads `.env` in the project directory both to fill `${DASHBOARD_PORT}` in the port mapping and to pass every setting into the container, so the host port and the port the server listens on stay the same.

To update to the latest code, pull the clone and rebuild:

```bash
cd /volume1/docker/sverdrup && git -C srv pull && docker compose up -d --build
```

For local development inside a clone, copy `docker-compose.example.yml` to `docker-compose.yml` in the repo root (it is git-ignored), change `build: ./srv` to `build: .`, and keep `.env` next to it; the database then lands in the repo's git-ignored `data/`.

Or without Compose:

```bash
docker build -t sverdrup:latest .
docker run -d --env-file .env -v "$PWD/data:/data" -p 8080:8080 sverdrup:latest
```

Navigate to `http://<host>:8080` (or your `DASHBOARD_PORT`) on any device on your home network.

## Manual Setup (without Docker)

### System Requirements
- Go 1.26+ (only to build the dashboard; its SQLite driver is pure Go, so no C compiler)
- Python 3.10+
- SQLite3

### Installation

1. Clone the repository:
```bash
git clone https://github.com/shaptonstahl/sverdrup.git
cd sverdrup
```

2. Set environment variables (or `set -a; . ./.env; set +a` to load your `.env`):
```bash
export SVERDRUP_ACCOUNTS="home"
export NEXTDNS_HOME_API_KEY="your_key"
export NEXTDNS_HOME_PROFILES="your_profile_id"
export DB_PATH="$PWD/sverdrup.db"
```

3. Build and run:
```bash
# Build Go server
cd server
go build -o sverdrup-server .

# The collector and processor need only the Python standard library
cd ..

# Start server (background)
./server/sverdrup-server &

# Start scheduler (runs collector/processor on schedule)
# (See Scheduler Setup below)
```

### Scheduler Setup

Use `cron` or `systemd` timers to run the collector and processor scripts:

#### Cron (example)

```bash
# Edit crontab
crontab -e

# Add these lines (cron does not read .env; set the variables in the crontab
# or in a wrapper script):
*/15 * * * * cd /path/to/sverdrup && python3 collector/collect.py
0 * * * * cd /path/to/sverdrup && python3 processor/process.py
```

#### Systemd timers (example)

Create `/etc/systemd/system/sverdrup-collect.service` and `.timer` files to manage scheduling.

## Reverse Proxy Configuration

### Nginx Example

```nginx
upstream sverdrup_backend {
    server 127.0.0.1:8080;
}

server {
    listen 443 ssl http2;
    server_name streaming.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    auth_basic "Sverdrup";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://sverdrup_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Caddy Example

```caddyfile
streaming.example.com {
    basicauth / {
        username hashedpassword
    }
    
    reverse_proxy 127.0.0.1:8080
}
```

## Data Persistence

- SQLite database file is stored at the path specified by `DB_PATH`
- In Docker: `/data` (default `DB_PATH=/data/sverdrup.db`) is bind-mounted from the project's `data/` directory (`/volume1/docker/sverdrup/data` on Miracle), so the database survives container rebuilds and lives outside the git clone
- Recommended backup strategy: daily snapshots of the database file

## Monitoring and Logs

### Docker Compose

```bash
# View logs (from the project directory)
docker compose logs -f sverdrup

# Collector and processor logs live inside the container
docker exec sverdrup tail -n 50 /var/log/sverdrup-collect.log
docker exec sverdrup tail -n 50 /var/log/sverdrup-process.log
```

### Manual Setup

- Server logs: stdout/stderr (redirect to a file if desired)
- Collector logs: check Python script output
- Database queries: SQLite provides query logging via `PRAGMA` statements

## Troubleshooting

### Dashboard not accessible
- Check that port 8080 is open and not blocked by a firewall
- Verify the server is running: `curl http://localhost:8080/health`
- A page saying "The database could not be read" usually means the server cannot open an existing database: it needs read access to the file and write access to its directory, because SQLite keeps its WAL shared-memory file (`sverdrup.db-shm`) next to it. The dashboard never writes data, but a read-only mount of the data directory does not work

### No data appearing
- "No data yet" (HTTP 503) means the database or its tables do not exist yet; the collector creates them on its first scheduled run
- Set the same `TZ` for the dashboard as for the processor: the processor's `TZ` defines each metrics day, and the dashboard's defines "today", this week (from Monday) and this month
- Check NextDNS accounts, keys and profile IDs in `.env`; the collector log names any variable it rejects
- Verify collector script ran: check logs
- Ensure SQLite database file has write permissions

### High memory usage
- Go server should be <50MB at idle
- Python jobs should exit cleanly after running
- Check for stuck processes: `ps aux | grep sverdrup`
