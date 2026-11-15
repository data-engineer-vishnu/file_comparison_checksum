# file_comparison_checksum

Row-by-row comparison of two CSV files that **cannot be moved to the same machine**
(e.g. MSSQL output on-prem vs BigQuery output in the cloud).

Each side turns its own raw CSV into a *checksum file* that contains **only the
primary key column(s) and one row checksum**. Only that file leaves the
premises. The cloud side compares the two checksum files and reports, per
primary key, whether the row matched, differed, or was missing on one side.

```
files/
  raw_files/              raw CSV exports (never leave their machine)
  checksum_files/
    onprem/               on-prem checksum files (brought over to the cloud machine)
    cloud/                cloud checksum files
  comparison_results/     one <name>_comparison.csv per compared pair
  logs/                   run summaries of both scripts
config/
  checksum_config.yaml    every raw file to process + its columns and normalization rules
  compare_config.yaml     which on-prem checksum file is compared with which cloud one
generate_checksum.py      STEP 1 - run on each machine (all files in checksum_config.yaml)
compare_checksums.py      STEP 2 - run on the cloud machine (all pairs in compare_config.yaml)
inspect_mismatches.py     optional root-cause helper (local only)
csv_checksum/             the library code (no need to edit)
tests/
```

## Setup

Python 3.11+ and PyYAML:

```bash
pip install -r requirements.txt
```

## How to run

There is no command line. Everything is driven by two YAML files:

* `config/checksum_config.yaml` lists the raw files with their column settings.
  `generate_checksum.py` uses it.
* `config/compare_config.yaml` maps each on-prem checksum file to its cloud
  counterpart. `compare_checksums.py` uses it.

The scripts have only a small `EDIT ME` block at the top that points to the YAML.

```
 ON-PREM (MSSQL export)                          CLOUD (BigQuery export)
 raw.csv                                          raw.csv
   │ generate_checksum.py                           │ generate_checksum.py
   ▼                                                ▼
 onprem_checksum.csv ───── transfer ─────────►   cloud_checksum.csv
 (Employee_ID, checksum)                            │ compare_checksums.py
                                                    ▼
                                  comparison_results/<name>_comparison.csv
                                  (key, status, both checksums) + run summary
```

### Step 1: `generate_checksum.py` (on each machine)

List every raw file in `config/checksum_config.yaml` (see [YAML config](#yaml-config)), then run:

```bash
python generate_checksum.py
```

This creates one checksum file per entry, at the entry's `output` location.
The `EDIT ME` block in the script has three settings:

```python
CONFIG_FILE  = ROOT / "config" / "checksum_config.yaml"
SUMMARY_FILE = ROOT / "files" / "logs" / "generate_summary_<timestamp>.csv"
ONLY = []          # e.g. ["sales_orders"] to (re)run only some entries
```

Progress is printed per file:

```
[1/200] employee_records: OK - 30000 rows, 30000 keys -> ...\employee_records_checksum.csv
[2/200] sales_orders: FAILED - Primary key column(s) not found in the input header: ['Order_ID'] ...
...
Done in 412.3s: 199 succeeded (3 with warnings), 1 failed
  FAILED #2 sales_orders: Primary key column(s) not found ...
```

* **One bad file does not stop the run.** A missing file, a missing column or a
  config mistake in one entry is reported, and the other files are still processed.
  Fix the problem, put the name in `ONLY`, and re-run just that file.
* **Failed files keep their old output.** Each checksum file is written to a temp
  file and swapped in only when complete, so a failed file never leaves a
  half-written checksum file. If a file fails, any checksum file from an earlier
  run is left as it was, and the script tells you so.
* **Run summary.** `SUMMARY_FILE` lists every file with its status
  (`OK`, `OK_WITH_WARNINGS` or `FAILED`), row and key counts, hashed columns,
  warnings, errors and duration. It contains no data values.
* **Exit code.** `0` if every file succeeded, `2` if any failed.

Each checksum file has one row per distinct primary key and nothing else:

```
Employee_ID,checksum
1,eca697904078986a5950ff45f7cc9f5988a418d65d49c8bd37437a1cb15d546c
2,52274a4a0c7d16f75994e199dbf1e425ddd9c296059a01cc36d01433e5dd1f35
```

The script prints a summary: row count, hashed columns, and warnings about
duplicate keys, rows with the wrong number of fields, or values that could not
be parsed. If a primary key occurs more than once, those rows are combined into
one checksum, which will then show as `MISMATCH` against the other side.

### Step 2: `compare_checksums.py` (on the cloud machine)

Copy the on-prem checksum files into one folder (e.g. `files/checksum_files/onprem/`)
and generate the cloud checksum files into another (e.g. `files/checksum_files/cloud/`).
Then map them in `config/compare_config.yaml`:

```yaml
source_dir: files/checksum_files/onprem    # on-prem checksum files
target_dir: files/checksum_files/cloud     # cloud checksum files
result_dir: files/comparison_results
source_label: onprem
target_label: cloud
only_differences: false                    # true = result files contain only non-MATCH rows
auto_match_same_names: true                # also pair files that have the same name in both folders

comparisons:                               # explicit pairs (needed when names differ)
  - name: employee_records                 # optional, also names the result file
    source: employee_records_checksum.csv  # relative to source_dir, or absolute
    target: employee_records_checksum.csv  # relative to target_dir, or absolute
    result: employee_records_comparison.csv  # optional, relative to result_dir
    enabled: true                          # optional
```

```bash
python compare_checksums.py
```

* **Explicit pairs.** Every entry under `comparisons` is one on-prem/cloud
  pair. Use these when file names differ between the two sides.
* **Auto-matching.** With `auto_match_same_names: true`, every file in `source_dir` that
  is not listed explicitly is paired with the file of the **same name**
  (case-insensitive) in `target_dir`. If both sides name their checksum files
  identically, you don't need to list all 200 files. Files with no same-named
  partner are reported as `NO_CLOUD_FILE` / `NO_ONPREM_FILE`.
* **Failures.** A pair that cannot be compared (missing file, different primary key
  columns, result file open in Excel, ...) is reported as `FAILED`, and the other
  pairs are still compared.
* **Re-running some pairs.** Set `ONLY = ["name", ...]` in the script.

Progress and the final summary are printed:

```
[1/200] employee_records: DIFFERENCES - 30000 keys (mismatch=16)
[2/200] sales_orders: ALL_MATCH - 120455 keys
...
Done in 35.2s:
  ALL_MATCH           : 196
  DIFFERENCES         : 2
  FAILED              : 1
  NO_CLOUD_FILE       : 1
```

`SUMMARY_FILE` (`files/logs/compare_summary_<timestamp>.csv`) has one row per pair:
name, both file paths, result file, status, and the counts of `match`,
`mismatch`, `missing_in_cloud` and `missing_in_onprem` keys, plus any error.

Exit code: `0` if every pair fully matches, `1` if any pair has differences or
a file has no partner, and `2` if any pair failed.

Each pair's result file (`comparison_results/<name>_comparison.csv`):

```
Employee_ID,status,onprem_checksum,cloud_checksum
1,MISMATCH,eca69790…,3766ddc9…
2,MATCH,52274a4a…,52274a4a…
```

| status | meaning |
|---|---|
| `MATCH` | the checksums are identical |
| `MISMATCH` | the key exists on both sides but the row data differs |
| `MISSING_IN_CLOUD` | the key exists only in the on-prem file |
| `MISSING_IN_ONPREM` | the key exists only in the cloud file |

In each checksum file, all columns before `checksum` are treated as the primary key, so
the compare step does not need `checksum_config.yaml`. Both files in a pair
must have the same primary key columns.

### Optional: `inspect_mismatches.py` (root cause, local only)

The checksum only tells you *that* a row differs. To see *why*, send the
comparison result back to the on-prem side (it contains only keys, status and hashes). Then
run this script on both machines. Set `FILE_NAME` to the entry's `name` in the
config; its input file and rules are used. Set `KEYS_FILE` to that file's
comparison result. It writes the **normalized** values (exactly
what was hashed) for every non-`MATCH` key in that machine's raw file. Compare
the two outputs side by side. The output also shows whether the data really differs
or whether a normalization rule in the YAML needs adjusting.

The inspect output contains real data, so keep it on the machine where it was created.

## YAML config

This section describes `config/checksum_config.yaml`. The compare config is
described under [Step 2](#step-2-compare_checksumspy-on-the-cloud-machine).
`config/checksum_config.yaml` lists every raw file to process. For each file
it sets **where** the input and output are, **which columns** are hashed, and
**how each value is normalized**, so that the MSSQL and BigQuery renderings of
the same value hash the same. Unknown keys are rejected so that typos are caught.

**Use the same columns and rules for a file on both machines.** The cloud
machine has its own copy of this YAML, with its own input paths. The checksum
file carries no config information, so if the two sides use different columns
or rules, every row of that file will come out as `MISMATCH`.

### Layout

```yaml
output_dir: files/checksum_files     # default folder when an entry has no 'output'

defaults:                            # optional: settings shared by every file
  null_handling: { null_token: "<<NULL>>", null_values: ["", "NULL"] }
  hash_algorithm: sha256
  csv: { delimiter: ",", encoding: utf-8-sig }

files:
  - name: employee_records           # optional, default = input file name without .csv
    input: files/raw_files/employee_records.csv
    output: files/checksum_files/onprem/employee_records_checksum.csv   # optional
    enabled: true                    # optional, false = skip
    primary_keys: [Employee_ID]
    include_columns: [Employee_Name, Salary, Joining_Date]
    exclude_columns: []
    column_rules:
      Salary: { type: decimal, scale: 2 }

  - name: sales_orders
    input: D:/exports/sales_orders.csv
    output: D:/checksums/sales_orders_checksum.csv
    primary_keys: [Order_ID, Line_No]
    include_columns: "*"
    ...
```

* **Paths.** Relative paths are resolved from the project folder. For Windows
  absolute paths, use forward slashes (`D:/exports/x.csv`) or single quotes
  (`'D:\exports\x.csv'`). In double quotes, YAML treats backslashes as escape characters.
* **Default output.** If `output` is left out, the checksum file goes to
  `<output_dir>/<input file name>_checksum.csv`.
* **Unique outputs.** Two entries may not write the same output file, and names must be unique.
* **Defaults.** Any checksum setting can go either in `defaults` or on each
  file. A setting on a file wins. `column_rules`, `null_handling` and `csv` are
  merged key by key, so a file can add or override one column rule and keep the rest.

### Checksum settings (per file or in `defaults`)

```yaml
primary_keys: [Employee_ID]          # one or more columns; matched, not hashed

include_columns:                     # cherry-pick columns, or: include_columns: "*"
  - Employee_Name
  - Salary
  - Joining_Date

exclude_columns: [load_timestamp]    # columns you genuinely cannot reconcile; wins over include

column_rules:                        # per-column normalization (default_rule for the rest)
  Salary:       { type: decimal, scale: 2 }
  Joining_Date: { type: date, input_formats: ["%Y-%m-%d", "%m/%d/%Y"] }

default_rule: { type: string, trim: true }

null_handling:
  null_token: "<<NULL>>"
  null_values: ["", "NULL"]

hash_algorithm: sha256               # sha256 | sha1 | md5 | sha512 | blake2b
on_parse_error: keep_raw             # keep_raw | error
case_insensitive_headers: true
csv: { delimiter: ",", quotechar: '"', encoding: utf-8-sig }
```

* Header names are matched case-insensitively. Column order in the file does not
  matter, because hashed columns are always sorted by name.
* Primary keys are normalized too, so `1` and `1.0` still match each other.

### Column types

| type | Use for | Options | Example: MSSQL / BigQuery → normalized |
|---|---|---|---|
| `string` (default) | VARCHAR / STRING | `trim` (true), `case` (`upper`/`lower`), `collapse_whitespace`, `unicode_form` (`NFC`) | ` abc ` → `abc` |
| `integer` | INT / BIGINT / INT64 | none | `5` / `5.0` → `5` |
| `decimal` | DECIMAL / MONEY / FLOAT / NUMERIC | `scale`, `rounding` (`half_up`, `half_even`, `truncate`) | `129690.0` / `129690` → `129690` |
| `boolean` | BIT / BOOL | `true_values`, `false_values` | `1` / `true` → `true` |
| `date` | DATE | `input_formats`, `output_format` | `2023-06-04 00:00:00.000` / `6/4/2023` → `2023-06-04` |
| `datetime` | DATETIME(2) / DATETIMEOFFSET / TIMESTAMP | `input_formats`, `precision` (`seconds`, `milliseconds`, `microseconds`) | `…10:15:30.123` / `…10:15:30.123456 UTC` → `2023-06-04 10:15:30` with `precision: seconds` |

* **FLOAT columns:** always set `scale`, because the two engines print floating-point values differently.
* **Datetime precision:** MSSQL `DATETIME` is accurate only to about 3 ms, so `precision: seconds` is usually safest.
  Timezone-aware values are converted to UTC.
* **Date formats** are tried in order. Never list both `%m/%d/%Y` and `%d/%m/%Y`.
* **Unparsable values:** with `keep_raw` they are hashed as-is, and the script prints how many there were and the first line numbers.

### NULL handling

Any value whose trimmed text is listed in `null_values` (case-insensitive) is
replaced by `null_token` before hashing. MSSQL exports usually write `NULL` or
an empty field, while BigQuery writes an empty field. Both become `<<NULL>>`.
Because CSV cannot tell an empty string from a NULL, both are treated as NULL by
default. If you need to distinguish them, remove `""` from `null_values`.

## Example with the sample files

`files/raw_files/employee_records_altered.csv` writes dates as `6/4/2023` instead
of `2023-06-04`, writes 309 salaries as `129690` instead of `129690.0`, and changes
16 rows. With `config/checksum_config.yaml`, the formatting differences are
normalized away. The sample `checksum_config.yaml` has both files as entries,
so a single `python generate_checksum.py` writes
`files/checksum_files/onprem/employee_records_checksum.csv` and
`files/checksum_files/cloud/employee_records_checksum.csv`. Then
`python compare_checksums.py` reports exactly the 16 changed rows:

```
[1/1] employee_records: DIFFERENCES - 30000 keys (mismatch=16)
```

## Tests

```bash
python -m unittest discover -s tests -v
```
