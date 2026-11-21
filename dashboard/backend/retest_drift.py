"""Record the measured Nautilus drift as the current allowance (lane NT2, DECISIONS #449, #452).

    python -m dashboard.backend.retest_drift --db <research.db> --set <drift.json>

Reads the two maxima from a drift.json written by research_lab.nautilus_drift.measure, appends one
research_retest_drift row through retestdb.set_drift_allowance (drift_source is the file name plus
the file's sha256), and prints that row. The newest row is the one record_retest reads. The morning
runs this on the server after the deploy; the lane never does.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from . import retestdb


def set_from_file(db_path: Path, drift_json: Path) -> dict:
    raw = drift_json.read_bytes()
    document = json.loads(raw.decode("utf-8"))
    for key in ("max_abs_gap_final_equity", "max_abs_gap_sharpe"):
        if key not in document:
            raise ValueError(f"{drift_json} has no {key}")
    source = f"{drift_json.name} sha256:{hashlib.sha256(raw).hexdigest()}"
    retestdb.init_retest_db(db_path)
    new_id = retestdb.set_drift_allowance(
        db_path, document["max_abs_gap_final_equity"], document["max_abs_gap_sharpe"], source)
    with retestdb.get_retest_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM research_retest_drift WHERE id = ?;", (new_id,)).fetchone()
    return dict(row)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--set", required=True, type=Path, dest="drift_json")
    args = parser.parse_args(argv)
    print(json.dumps(set_from_file(args.db, args.drift_json), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
