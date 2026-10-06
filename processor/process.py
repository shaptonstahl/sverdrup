#!/usr/bin/env python3
"""
Processor: Sessionizes DNS queries and computes metrics.

Environment variables:
  DB_PATH: SQLite database path (default: /data/sverdrup.db)
"""

import os
import sys

def main():
    db_path = os.getenv("DB_PATH", "/data/sverdrup.db")

    # TODO: Implement processor logic
    # 1. Open SQLite database
    # 2. Read unprocessed queries from 'queries' table
    # 3. Match domains to streaming services
    # 4. Sessionize: group queries by service, device, and time gaps (e.g., 15 min)
    # 5. Calculate session lengths
    # 6. Aggregate by date: compute daily metrics (total_minutes, session_count)
    # 7. Insert into 'sessions' and 'metrics' tables
    # 8. Mark queries as processed
    # 9. Exit gracefully

    print(f"Processor: Using database at {db_path}")
    print("TODO: Implement processor")

if __name__ == "__main__":
    main()
