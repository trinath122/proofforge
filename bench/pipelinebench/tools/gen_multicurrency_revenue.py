"""Generate the `multicurrency_revenue_rollup` PipelineBench case deterministically.

Usage (from the repo root):  uv run python bench/pipelinebench/tools/gen_multicurrency_revenue.py

Writes visible data (3 months, clean region-change boundaries) and hidden holdout data
(3 later months, more currencies, mid-month region changes, orders on boundary days),
then computes golden expectations by running solution.sql through the same runner the
gates use.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import random
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "bench" / "pipelinebench" / "cases" / "multicurrency_revenue_rollup"
sys.path.insert(0, str(ROOT / "src" / "proofforge" / "playbooks" / "assets"))
from run_pipeline import load  # noqa: E402  (the sandbox runner: identical type inference)

REGIONS = ["na", "emea", "apac", "latam"]
OUTPUT = "monthly_region_revenue"
COLUMNS = ["month", "region", "orders", "gross_usd", "refunds_usd", "net_usd"]


def days(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(d) for d in range((end - start).days + 1)]


def generate(
    seed: int,
    start: dt.date,
    end: dt.date,
    n_customers: int,
    n_orders: int,
    currencies: dict[str, float],
    mid_month_changes: bool,
) -> dict[str, list[list[object]]]:
    rng = random.Random(seed)
    all_days = days(start, end)

    # FX: business days only, starting well before the first order; USD is never listed.
    fx_rows: list[list[object]] = []
    rates = dict(currencies)
    for day in days(start - dt.timedelta(10), end):
        if day.weekday() >= 5:
            continue
        for cur in sorted(rates):
            rates[cur] = round(
                rates[cur] * (1 + rng.uniform(-0.01, 0.01)), 4 if rates[cur] > 0.05 else 6
            )
            fx_rows.append([day.isoformat(), cur, rates[cur]])

    # Customers: SCD2 history; some move region during the period; some are missing entirely.
    cust_rows: list[list[object]] = []
    months = sorted({d.replace(day=1) for d in all_days})
    change_days: dict[int, list[dt.date]] = {}
    for cid in range(1, n_customers + 1):
        region = rng.choice(REGIONS)
        valid_from = start - dt.timedelta(days=rng.randint(30, 400))
        changes: list[dt.date] = []
        if rng.random() < 0.3:
            if mid_month_changes:
                changes = sorted(rng.sample(all_days[5:-5], k=rng.randint(1, 2)))
            else:
                changes = [rng.choice(months[1:])]
        for change in changes:
            cust_rows.append([cid, region, valid_from.isoformat(), change.isoformat()])
            region = rng.choice([r for r in REGIONS if r != region])
            valid_from = change
        cust_rows.append([cid, region, valid_from.isoformat(), ""])
        change_days[cid] = changes
    ghost_customers = list(range(n_customers + 1, n_customers + 6))

    # Orders with at-least-once ingestion: duplicates, and later re-ingestions that cancel.
    order_rows: list[list[object]] = []
    refund_rows: list[list[object]] = []
    refund_id = 1
    cur_names = ["USD", *sorted(currencies)]
    for oid in range(1, n_orders + 1):
        cid = rng.choice(ghost_customers) if rng.random() < 0.03 else rng.randint(1, n_customers)
        if mid_month_changes and change_days.get(cid) and rng.random() < 0.5:
            day = rng.choice(change_days[cid])  # lands exactly on a region boundary
        elif mid_month_changes:
            day = rng.choice(all_days)
        else:
            day = rng.choice([d for d in all_days if d.day != 1])
        ts = dt.datetime.combine(
            day, dt.time(rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
        )
        currency = rng.choice(cur_names)
        amount = round(rng.uniform(5, 900) * (150 if currency == "JPY" else 1), 2)
        ingested = ts + dt.timedelta(minutes=rng.randint(1, 30))
        copies = 1 + (rng.random() < 0.2) + (rng.random() < 0.05)
        cancelled_later = rng.random() < 0.06
        for k in range(copies):
            order_rows.append(
                [
                    oid,
                    cid,
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    currency,
                    amount,
                    "completed",
                    (ingested + dt.timedelta(hours=k)).strftime("%Y-%m-%d %H:%M:%S"),
                ]
            )
        if cancelled_later:
            order_rows.append(
                [
                    oid,
                    cid,
                    ts.strftime("%Y-%m-%d %H:%M:%S"),
                    currency,
                    amount,
                    "cancelled",
                    (ingested + dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
                ]
            )
        if rng.random() < 0.15:
            remaining = amount
            for _ in range(rng.randint(1, 3)):
                part = round(remaining * rng.uniform(0.1, 0.5), 2)
                remaining = round(remaining - part, 2)
                refund_ts = ts + dt.timedelta(days=rng.randint(1, 45))  # often next month
                refund_rows.append([refund_id, oid, refund_ts.strftime("%Y-%m-%d %H:%M:%S"), part])
                refund_id += 1
    rng.shuffle(order_rows)
    return {
        "orders_ingest": [
            ["order_id", "customer_id", "order_ts", "currency", "amount", "status", "_ingested_at"],
            *order_rows,
        ],
        "customers": [["customer_id", "region", "valid_from", "valid_to"], *cust_rows],
        "fx_rates": [["date", "currency", "usd_rate"], *fx_rows],
        "refunds": [["refund_id", "order_id", "refund_ts", "amount"], *refund_rows],
    }


def write_csvs(folder: Path, tables: dict[str, list[list[object]]]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.csv"):
        old.unlink()
    for name, rows in tables.items():
        with (folder / f"{name}.csv").open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle, lineterminator="\n").writerows(rows)


def expectations(folder: Path, solution: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        db = sqlite3.connect(Path(tmp) / "w.db")
        load(db, folder)
        db.executescript(solution)
        rows = [
            list(r)
            for r in db.execute(f"SELECT {', '.join(COLUMNS)} FROM {OUTPUT} ORDER BY month, region")
        ]
    completed = (
        "SELECT COUNT(*) FROM (SELECT status, ROW_NUMBER() OVER (PARTITION BY order_id "
        "ORDER BY _ingested_at DESC) AS rn FROM raw_orders_ingest) WHERE rn = 1 AND status = 'completed'"
    )
    return {
        "tables": {
            OUTPUT: {
                "columns": COLUMNS,
                "not_null": COLUMNS,
                "unique": [["month", "region"]],
                "accepted_values": {"region": [*REGIONS, "unknown"]},
                "golden": {
                    "key": ["month", "region"],
                    "columns": COLUMNS,
                    "rows": rows,
                    "tolerance": 0.011,
                },
                "reconcile": [
                    {
                        "name": "each completed order counted once",
                        "actual": f"SELECT SUM(orders) FROM {OUTPUT}",
                        "expected": completed,
                    },
                    {
                        "name": "net = gross - refunds",
                        "actual": f"SELECT ROUND(SUM(net_usd), 2) FROM {OUTPUT}",
                        "expected": f"SELECT ROUND(SUM(gross_usd) - SUM(refunds_usd), 2) FROM {OUTPUT}",
                        "tolerance": 0.05,
                    },
                ],
            }
        }
    }


def main() -> None:
    solution = (CASE / "solution.sql").read_text(encoding="utf-8")
    visible = generate(
        seed=7,
        start=dt.date(2026, 6, 1),
        end=dt.date(2026, 8, 31),
        n_customers=150,
        n_orders=400,
        currencies={"EUR": 1.08, "GBP": 1.27},
        mid_month_changes=False,
    )
    hidden = generate(
        seed=91,
        start=dt.date(2026, 9, 1),
        end=dt.date(2026, 11, 30),
        n_customers=220,
        n_orders=1500,
        currencies={"EUR": 1.10, "GBP": 1.25, "JPY": 0.0068, "CAD": 0.73},
        mid_month_changes=True,
    )
    write_csvs(CASE / "data", visible)
    write_csvs(CASE / "holdout" / "data", hidden)
    for folder, target in (
        (CASE / "data", CASE / "expectations.json"),
        (CASE / "holdout" / "data", CASE / "holdout" / "expectations.json"),
    ):
        target.write_text(
            json.dumps(expectations(folder, solution), indent=2) + "\n", encoding="utf-8"
        )
    print("generated", CASE)


if __name__ == "__main__":
    main()
