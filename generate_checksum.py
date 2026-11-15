"""STEP 1 - run on EACH machine (on-prem and cloud).

Creates one checksum file per entry in the YAML config. Each checksum file
contains ONLY the primary key column(s) and the row checksum. The input/output
locations and the column settings of every file are in the YAML.

Edit the block below if needed, then run:   python generate_checksum.py

A file that fails (missing file, missing column, ...) is reported and skipped;
the remaining files are still processed. A summary of the run is printed and
written to SUMMARY_FILE.
"""

import sys
from datetime import datetime
from pathlib import Path

from csv_checksum.runner import run_generate_all

ROOT = Path(__file__).resolve().parent

# ----------------------------------------------------------------- EDIT ME --
CONFIG_FILE = ROOT / "config" / "checksum_config.yaml"

# Run summary (file names, row counts, status - no data values).
SUMMARY_FILE = ROOT / "files" / "logs" / f"generate_summary_{datetime.now():%Y%m%d_%H%M%S}.csv"

# Leave empty to process every enabled entry, or list names to run only those,
# e.g. ONLY = ["employee_records"]  (handy for re-running files that failed).
ONLY = []
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(run_generate_all(CONFIG_FILE, ROOT, summary_file=SUMMARY_FILE, only=ONLY))
