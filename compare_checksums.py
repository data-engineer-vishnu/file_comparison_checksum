"""STEP 2 - run on the cloud machine.

Compares every on-prem checksum file with its cloud counterpart, as mapped in
the compare YAML, and writes one result CSV per pair:

    <primary keys>, status, onprem_checksum, cloud_checksum

status is one of MATCH, MISMATCH, MISSING_IN_CLOUD, MISSING_IN_ONPREM.

A pair that cannot be compared (missing file, different primary keys, ...)
is reported and skipped; the remaining pairs are still compared. A summary of
all pairs is printed and written to SUMMARY_FILE.

Edit the block below if needed, then run:   python compare_checksums.py
"""

import sys
from datetime import datetime
from pathlib import Path

from csv_checksum.runner import run_compare_all

ROOT = Path(__file__).resolve().parent

# ----------------------------------------------------------------- EDIT ME --
CONFIG_FILE = ROOT / "config" / "compare_config.yaml"

# One row per pair: counts of match / mismatch / missing keys, status, errors.
SUMMARY_FILE = ROOT / "files" / "logs" / f"compare_summary_{datetime.now():%Y%m%d_%H%M%S}.csv"

# Leave empty to compare every enabled pair, or list names to run only those,
# e.g. ONLY = ["employee_records"].
ONLY = []
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(run_compare_all(CONFIG_FILE, ROOT, summary_file=SUMMARY_FILE, only=ONLY))
