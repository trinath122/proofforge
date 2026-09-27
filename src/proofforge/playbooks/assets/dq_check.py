"""Data-quality gate executed inside the sandbox (standard library only).

Checks tables in warehouse.db against a JSON expectation file and exits 1 on any
failure. Usage: python dq_check.py <expectations.json>

Supported expectations per table:
  columns          exact ordered column list
  row_count        exact number of rows
  not_null         columns that must never be NULL
  unique           list of column groups that must be unique
  accepted_values  {column: [allowed values]}
  golden           {"key": [cols], "columns": [cols], "rows": [[...], ...]}  exact contents
  reconcile        [{"name", "actual", "expected", "tolerance"}]  two scalar SQL queries
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

FLOAT_TOL = 1e-6


def same(a: Any, b: Any, tol: float = FLOAT_TOL) -> bool:
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) <= tol
    return bool(a == b)


class Checker:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.failures = 0

    def report(self, ok: bool, label: str, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {label}{f'  -> {detail}' if detail and not ok else ''}")
        self.failures += 0 if ok else 1

    def scalar(self, sql: str) -> Any:
        return self.db.execute(sql).fetchone()[0]

    def table(self, name: str, spec: dict[str, Any]) -> None:
        q = f'"{name}"'
        cols = [row[1] for row in self.db.execute(f"PRAGMA table_info({q})")]
        self.report(bool(cols), f"{name}: table exists", "missing")
        if not cols:
            return
        if "columns" in spec:
            self.report(cols == spec["columns"], f"{name}: columns", f"got {cols}")
        if "row_count" in spec:
            n = self.scalar(f"SELECT COUNT(*) FROM {q}")  # noqa: S608
            self.report(
                n == spec["row_count"], f"{name}: row_count={spec['row_count']}", f"got {n}"
            )
        for col in spec.get("not_null", []):
            n = self.scalar(f'SELECT COUNT(*) FROM {q} WHERE "{col}" IS NULL')  # noqa: S608
            self.report(n == 0, f"{name}: {col} not null", f"{n} null rows")
        for group in spec.get("unique", []):
            key = ", ".join(f'"{c}"' for c in group)
            dupes = self.scalar(
                f"SELECT COUNT(*) FROM (SELECT {key} FROM {q} GROUP BY {key} HAVING COUNT(*) > 1)"  # noqa: S608
            )
            self.report(
                dupes == 0, f"{name}: unique({', '.join(group)})", f"{dupes} duplicate keys"
            )
        for col, allowed in spec.get("accepted_values", {}).items():
            seen = {r[0] for r in self.db.execute(f'SELECT DISTINCT "{col}" FROM {q}')}  # noqa: S608
            extra = sorted(map(str, seen - set(allowed)))
            self.report(not extra, f"{name}: {col} accepted values", f"unexpected {extra}")
        if "golden" in spec:
            self.golden(name, q, spec["golden"])
        for rec in spec.get("reconcile", []):
            actual, expected = self.scalar(rec["actual"]), self.scalar(rec["expected"])
            tol = float(rec.get("tolerance", FLOAT_TOL))
            ok = actual is not None and expected is not None and same(actual, expected, tol)
            self.report(ok, f"{name}: reconcile {rec['name']}", f"{actual} != {expected}")

    def golden(self, name: str, q: str, gold: dict[str, Any]) -> None:
        cols = ", ".join(f'"{c}"' for c in gold["columns"])
        key = ", ".join(f'"{c}"' for c in gold["key"])
        got = [list(r) for r in self.db.execute(f"SELECT {cols} FROM {q} ORDER BY {key}")]  # noqa: S608
        want = gold["rows"]
        if len(got) != len(want):
            self.report(False, f"{name}: golden rows", f"expected {len(want)} rows, got {len(got)}")
            return
        for g, w in zip(got, want, strict=True):
            if not all(same(a, b) for a, b in zip(g, w, strict=True)):
                self.report(False, f"{name}: golden rows", f"first mismatch: got {g}, want {w}")
                return
        self.report(True, f"{name}: golden rows")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python dq_check.py <expectations.json>", file=sys.stderr)
        return 2
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    db = sqlite3.connect("warehouse.db")
    checker = Checker(db)
    for name, table_spec in spec["tables"].items():
        checker.table(name, table_spec)
    print(f"{checker.failures} data-quality check(s) failed")
    return 1 if checker.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
