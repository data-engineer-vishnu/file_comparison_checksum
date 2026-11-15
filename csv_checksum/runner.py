"""Entry points used by the top-level scripts (generate_checksum.py etc.).

Each function prints a summary and returns a process exit code:
0 = success / all rows match, 1 = differences found, 2 = error.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from .compare_jobs import load_compare_plan
from .comparer import MATCH, MISMATCH, compare
from .jobs import find_job, load_jobs
from .generator import generate, inspect
from .normalizers import ConfigError, NormalizationError


def _guard(func):
    def wrapper(*args, **kwargs) -> int:
        try:
            return func(*args, **kwargs)
        except (ConfigError, NormalizationError) as exc:
            print(f"ERROR: {exc}")
            return 2
        except PermissionError as exc:
            print(f"ERROR: cannot write {exc.filename} - is it open in Excel or another program? Close it and re-run.")
            return 2

    return wrapper


def _warnings(res) -> list[str]:
    out = []
    if res.duplicate_keys:
        out.append(f"{res.duplicate_keys} primary key(s) occur more than once; their rows were combined into one checksum")
    if res.stats.ragged_rows:
        out.append(f"{res.stats.ragged_rows} row(s) have a different number of fields than the header "
                   f"(first lines: {res.stats.ragged_row_lines})")
    for col, n in res.stats.parse_errors.items():
        out.append(f"{n} value(s) in '{col}' could not be parsed as configured and were hashed as-is "
                   f"(first lines: {res.stats.parse_error_lines[col]})")
    return out


@_guard
def run_generate_all(
    jobs_file: str | Path,
    project_dir: str | Path,
    summary_file: str | Path | None = None,
    only: list[str] | None = None,
) -> int:
    """Generate a checksum file for every enabled entry in the multi-file YAML.

    A failing file is reported and skipped; the others still run.
    Returns 0 if every selected file succeeded, 2 otherwise.
    """
    jobs = load_jobs(jobs_file, project_dir)
    if only:
        wanted = {n.casefold() for n in only}
        unknown = wanted - {j.name.casefold() for j in jobs}
        if unknown:
            raise ConfigError(f"ONLY lists unknown name(s): {sorted(unknown)}")
        selected = [j for j in jobs if j.name.casefold() in wanted]
    else:
        selected = [j for j in jobs if j.enabled]
    skipped = len(jobs) - len(selected)

    print(f"Config           : {jobs_file}")
    print(f"Files to process : {len(selected)} (skipped/disabled: {skipped})")
    print()

    rows = []  # for the summary CSV
    started = time.perf_counter()
    for n, job in enumerate(selected, start=1):
        prefix = f"[{n}/{len(selected)}] {job.name}"
        t0 = time.perf_counter()
        record = {"number": job.number, "name": job.name, "input": job.input or "", "output": job.output or "",
                  "status": "", "input_rows": "", "distinct_keys": "", "hashed_columns": "",
                  "warnings": "", "error": "", "seconds": ""}
        try:
            if job.error:
                raise ConfigError(job.error)
            res = generate(job.config, job.input, job.output)
            warns = _warnings(res)
            record.update(status="OK_WITH_WARNINGS" if warns else "OK", input_rows=res.input_rows,
                          distinct_keys=res.distinct_keys, hashed_columns=";".join(res.layout.hashed_names),
                          warnings=" | ".join(warns))
            print(f"{prefix}: OK - {res.input_rows} rows, {res.distinct_keys} keys -> {res.output_path}")
            for w in warns:
                print(f"    WARNING: {w}")
        except Exception as exc:  # keep going with the other files
            msg = _error_text(exc)
            record.update(status="FAILED", error=msg)
            print(f"{prefix}: FAILED - {msg}")
            if job.output and Path(job.output).exists():
                print(f"    NOTE: {job.output} is from an earlier run and was NOT updated")
        record["seconds"] = round(time.perf_counter() - t0, 1)
        rows.append(record)

    ok = sum(r["status"].startswith("OK") for r in rows)
    warned = sum(r["status"] == "OK_WITH_WARNINGS" for r in rows)
    failed = [r for r in rows if r["status"] == "FAILED"]
    print()
    print(f"Done in {time.perf_counter() - started:.1f}s: {ok} succeeded ({warned} with warnings), {len(failed)} failed")
    for r in failed:
        print(f"  FAILED #{r['number']} {r['name']}: {r['error']}")

    if summary_file:
        _write_summary(summary_file, rows)
    return 0 if not failed else 2


@_guard
def run_compare(
    source_file: str | Path,
    target_file: str | Path,
    result_file: str | Path,
    source_label: str = "onprem",
    target_label: str = "cloud",
    only_differences: bool = False,
) -> int:
    res = compare(
        Path(source_file),
        Path(target_file),
        Path(result_file),
        source_label=source_label,
        target_label=target_label,
        only_differences=only_differences,
    )
    print(f"Keys compared    : {sum(res.counts.values())}")
    for status in sorted(res.counts, key=lambda s: (s != MATCH, s)):
        print(f"  {status:<20}: {res.counts[status]}")
    if res.sample_differences:
        print("First differences:")
        for pk, status in res.sample_differences[:10]:
            print(f"  {status:<20} {', '.join(pk)}")
    print(f"Result file      : {res.output_path}")
    print("RESULT: ALL ROWS MATCH" if res.all_match else "RESULT: DIFFERENCES FOUND")
    return 0 if res.all_match else 1


def _error_text(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return f"cannot write {exc.filename} - is it open in Excel or another program?"
    return str(exc) or exc.__class__.__name__


def _write_summary(summary_file, rows: list[dict]) -> None:
    summary_file = Path(summary_file)
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for r in rows:
        fieldnames += [k for k in r if k not in fieldnames]
    with open(summary_file, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames or ["status"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Summary file     : {summary_file}")


@_guard
def run_compare_all(
    config_file: str | Path,
    project_dir: str | Path,
    summary_file: str | Path | None = None,
    only: list[str] | None = None,
) -> int:
    """Compare every on-prem/cloud checksum file pair listed in the compare YAML.

    Returns 0 if every pair fully matches, 1 if any pair has differences
    (or a checksum file has no partner), 2 if any pair could not be compared.
    """
    plan = load_compare_plan(config_file, project_dir)
    s, t = plan.source_label, plan.target_label
    pairs = plan.pairs
    if only:
        wanted = {n.casefold() for n in only}
        unknown = wanted - {p.name.casefold() for p in pairs}
        if unknown:
            raise ConfigError(f"ONLY lists unknown name(s): {sorted(unknown)}")
        selected = [p for p in pairs if p.name.casefold() in wanted]
    else:
        selected = [p for p in pairs if p.enabled]

    print(f"Config           : {config_file}")
    print(f"Pairs to compare : {len(selected)} (listed: {sum(not p.auto_matched for p in pairs)}, "
          f"auto-matched: {sum(p.auto_matched for p in pairs)}, skipped/disabled: {len(pairs) - len(selected)})")
    print()

    missing_t, missing_s = f"missing_in_{t}", f"missing_in_{s}"
    status_map = {MATCH: "match", MISMATCH: "mismatch",
                  f"MISSING_IN_{t.upper()}": missing_t, f"MISSING_IN_{s.upper()}": missing_s}
    rows: list[dict] = []
    started = time.perf_counter()
    for n, pair in enumerate(selected, start=1):
        prefix = f"[{n}/{len(selected)}] {pair.name}"
        record = {"name": pair.name, f"{s}_file": pair.source or "", f"{t}_file": pair.target or "",
                  "result_file": pair.result or "", "status": "", "keys": "", "match": "", "mismatch": "",
                  missing_t: "", missing_s: "", "error": ""}
        try:
            if pair.error:
                raise ConfigError(pair.error)
            res = compare(pair.source, pair.target, pair.result, source_label=s, target_label=t,
                          only_differences=plan.only_differences)
            counts = {status_map[k]: v for k, v in res.counts.items()}
            record.update({k: counts.get(k, 0) for k in ("match", "mismatch", missing_t, missing_s)})
            record["keys"] = sum(res.counts.values())
            record["status"] = "ALL_MATCH" if res.all_match else "DIFFERENCES"
            detail = ", ".join(f"{k}={record[k]}" for k in ("mismatch", missing_t, missing_s) if record[k])
            print(f"{prefix}: {record['status']} - {record['keys']} keys" + (f" ({detail})" if detail else ""))
        except Exception as exc:  # keep going with the other pairs
            record.update(status="FAILED", error=_error_text(exc))
            print(f"{prefix}: FAILED - {record['error']}")
        rows.append(record)

    if not only:
        for f in plan.source_only:
            rows.append({"name": f.stem, f"{s}_file": f, "status": f"NO_{t.upper()}_FILE",
                         "error": f"no file with the same name in the {t} folder"})
        for f in plan.target_only:
            rows.append({"name": f.stem, f"{t}_file": f, "status": f"NO_{s.upper()}_FILE",
                         "error": f"no file with the same name in the {s} folder"})

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    print()
    print(f"Done in {time.perf_counter() - started:.1f}s:")
    for status in sorted(by_status, key=lambda x: (x != "ALL_MATCH", x)):
        print(f"  {status:<20}: {by_status[status]}")
    problems = [r for r in rows if r["status"] != "ALL_MATCH"]
    for r in problems[:50]:
        reason = r["error"] or ", ".join(
            f"{k}={r[k]}" for k in ("mismatch", missing_t, missing_s) if r.get(k))
        print(f"  {r['status']:<20} {r['name']}: {reason}")
    if len(problems) > 50:
        print(f"  ... and {len(problems) - 50} more (see the summary file)")
    if summary_file:
        _write_summary(summary_file, rows)

    if by_status.get("FAILED"):
        print("RESULT: SOME PAIRS COULD NOT BE COMPARED")
        return 2
    if problems:
        print("RESULT: DIFFERENCES FOUND")
        return 1
    print("RESULT: ALL FILES MATCH")
    return 0


def _read_keys_file(path: Path, pk_names: list[str], include_matches: bool) -> set[tuple[str, ...]]:
    if not path.exists():
        raise ConfigError(f"Keys file not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = {f.casefold(): f for f in reader.fieldnames or []}
        missing = [p for p in pk_names if p.casefold() not in fields]
        if missing:
            raise ConfigError(f"{path} does not contain primary key column(s) {missing}")
        cols = [fields[p.casefold()] for p in pk_names]
        status_col = fields.get("status")
        keys = set()
        for row in reader:
            if status_col and not include_matches and row[status_col] == MATCH:
                continue
            keys.add(tuple(row[c] for c in cols))
    return keys


@_guard
def run_inspect(
    jobs_file: str | Path,
    project_dir: str | Path,
    name: str,
    output_file: str | Path,
    keys_file: str | Path | None = None,
    keys: list | None = None,
    include_matches: bool = False,
) -> int:
    job = find_job(load_jobs(jobs_file, project_dir), name)
    config = job.config
    wanted: set[tuple[str, ...]] = set()
    if keys_file:
        wanted |= _read_keys_file(Path(keys_file), config.primary_keys, include_matches)
    for k in keys or []:
        parts = tuple(str(v) for v in k) if isinstance(k, (list, tuple)) else (str(k),)
        if len(parts) != len(config.primary_keys):
            raise ConfigError(
                f"Key {k!r} has {len(parts)} part(s) but there are {len(config.primary_keys)} primary key column(s)"
            )
        wanted.add(parts)
    if not wanted:
        print("Nothing to inspect: the keys file has no differences and no KEYS were given.")
        return 0
    found = inspect(config, job.input, wanted, Path(output_file))
    print(f"Input file       : {job.input}")
    print(f"Keys requested   : {len(wanted)}")
    print(f"Rows written     : {found}")
    print(f"Inspect file     : {output_file}")
    print("NOTE: the inspect file contains normalized DATA values - keep it on this machine.")
    return 0
