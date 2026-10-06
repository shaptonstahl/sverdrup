#!/usr/bin/env python3
"""
Collector: Fetches DNS logs from NextDNS API and stores in SQLite.

Environment variables:
  NEXTDNS_API_KEY: NextDNS API key
  NEXTDNS_PROFILE_ID: NextDNS profile ID
  NEXTDNS_API_URL: NextDNS API endpoint (default: https://api.nextdns.io)
  DB_PATH: SQLite database path (default: /data/sverdrup.db)
"""

import os
import sys

def main():
    api_key = os.getenv("NEXTDNS_API_KEY")
    profile_id = os.getenv("NEXTDNS_PROFILE_ID")
    db_path = os.getenv("DB_PATH", "/data/sverdrup.db")

    if not api_key or not profile_id:
        print("Error: NEXTDNS_API_KEY and NEXTDNS_PROFILE_ID environment variables required", file=sys.stderr)
        sys.exit(1)

    # TODO: Implement collector logic
    # 1. Initialize/open SQLite database
    # 2. Check if backfill has been done (metadata table)
    # 3. If not: fetch all available logs from NextDNS
    # 4. If yes: fetch logs since last known timestamp
    # 5. Insert into 'queries' table
    # 6. Update metadata with latest timestamp
    # 7. Exit gracefully

    print(f"Collector: Using database at {db_path}")
    print("TODO: Implement collector")

if __name__ == "__main__":
    main()
