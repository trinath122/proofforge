"""Pipeline runner executed inside the sandbox (standard library only).

Loads every CSV in a data directory into SQLite as `raw_<file stem>`, then runs
pipeline.sql against it. Usage: python run_pipeline.py <data_dir>
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

DB = Path("warehouse.db")
PIPELINE = Path("pipeline.sql")

Cell = int | float | str | None


def infer(value: str) -> Cell:
    """Empty -> NULL, then int, then float, else text."""
    if value == "":
        return None
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


def load(db: sqlite3.Connection, data_dir: Path) -> None:
    files = sorted(data_dir.glob("*.csv"))
    if not files:
        raise SystemExit(f"no CSV files found in {data_dir}")
    for path in files:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            rows = [[infer(v) for v in row] for row in reader]
        table = f'"raw_{path.stem}"'
        columns = ", ".join(f'"{c}"' for c in header)
        db.execute(f"CREATE TABLE {table} ({columns})")
        marks = ", ".join("?" for _ in header)
        db.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)  # noqa: S608
        print(f"loaded {len(rows):>5} rows into raw_{path.stem}")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python run_pipeline.py <data_dir>", file=sys.stderr)
        return 2
    DB.unlink(missing_ok=True)
    db = sqlite3.connect(DB)
    load(db, Path(sys.argv[1]))
    try:
        db.executescript(PIPELINE.read_text(encoding="utf-8"))
    except sqlite3.Error as exc:
        print(f"PIPELINE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    db.commit()
    print("pipeline.sql completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
