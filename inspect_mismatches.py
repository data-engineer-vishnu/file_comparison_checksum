"""OPTIONAL - root-cause helper, run locally on either machine.

Writes the normalized values (exactly what went into the checksum) of the
mismatched rows from THIS machine's raw file. Run it on both machines and
compare the two outputs side by side to see which column differs.

The output contains real data - keep it on the machine where it was created.

Edit the paths below, then run:   python inspect_mismatches.py
"""

import sys
from pathlib import Path

from csv_checksum.runner import run_inspect

ROOT = Path(__file__).resolve().parent

# ----------------------------------------------------------------- EDIT ME --
CONFIG_FILE = ROOT / "config" / "checksum_config.yaml"
FILE_NAME = "employee_records"   # the entry's "name" in the config (its input file and rules are used)
OUTPUT_FILE = ROOT / "files" / "raw_files" / "employee_records_inspect.csv"

# Comparison result from compare_checksums.py: every non-MATCH key is inspected.
KEYS_FILE = ROOT / "files" / "comparison_results" / "employee_records_comparison.csv"
# And/or list keys explicitly, as they appear in the checksum file.
# Single key: ["1", "14"]   Composite key: [("1", "EU"), ("14", "US")]
KEYS = []
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(run_inspect(CONFIG_FILE, ROOT, FILE_NAME, OUTPUT_FILE, keys_file=KEYS_FILE, keys=KEYS))
