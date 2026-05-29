#!/usr/bin/env python3
"""Quiet Kanban DB health watchdog.

Prints nothing when healthy. Emits a compact diagnostic when the configured
Kanban DB fails integrity checks. For index-only failures it attempts REINDEX
repair after backing up the DB family.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import kanban_db as kb  # noqa: E402


def _rows(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    finally:
        conn.close()


def main() -> int:
    path = kb.kanban_db_path()
    if not path.exists() or path.stat().st_size == 0:
        print(json.dumps({"status": "missing", "db_path": str(path)}))
        return 1
    before = _rows(path)
    if before == ["ok"]:
        return 0
    index_only = all(row.startswith("wrong # of entries in index ") for row in before)
    backup = kb._backup_corrupt_db(path)
    after = before
    repaired = False
    if index_only:
        conn = sqlite3.connect(path)
        try:
            conn.execute("REINDEX")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            after = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
            repaired = after == ["ok"]
        finally:
            conn.close()
    print(json.dumps({
        "status": "repaired" if repaired else "failed",
        "db_path": str(path),
        "backup_path": str(backup) if backup else None,
        "index_only": index_only,
        "before": before,
        "after": after,
    }, indent=2))
    return 0 if repaired else 2


if __name__ == "__main__":
    raise SystemExit(main())
