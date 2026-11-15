"""Multi-file YAML: one entry per raw CSV to turn into a checksum file.

    output_dir: files/checksum_files      # default folder for outputs
    defaults:                             # optional - applied to every file
      null_handling: {...}
      csv: {...}
    files:
      - name: employee_records            # optional, default = input file name without .csv
        input: files/raw_files/employee_records.csv
        output: files/checksum_files/employee_records_checksum.csv   # optional
        enabled: true                     # optional, false = skip this file
        primary_keys: [...]
        include_columns: [...]
        exclude_columns: [...]
        column_rules: {...}
        ...                               # any per-file setting

Merging an entry with ``defaults``:
  * ``column_rules``, ``null_handling`` and ``csv`` are merged key by key
    (a column rule given in the entry replaces the default rule for that column)
  * every other setting given in the entry replaces the default

Relative paths are resolved against ``base_dir`` (the project folder unless
the YAML sets ``base_dir``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Config, build_config
from .normalizers import ConfigError

_TOP_KEYS = {"base_dir", "output_dir", "defaults", "files"}
_JOB_KEYS = {"name", "input", "output", "enabled"}
_MERGED_SECTIONS = {"column_rules", "null_handling", "csv"}


@dataclass
class Job:
    number: int  # 1-based position in the files list
    name: str
    input: Path | None
    output: Path | None
    enabled: bool
    config: Config | None
    error: str | None = None

    @property
    def label(self) -> str:
        return f"#{self.number} {self.name}"


def _merge(defaults: dict, item: dict) -> dict:
    merged = dict(defaults)
    for key, value in item.items():
        if key in _MERGED_SECTIONS and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _resolve(base: Path, value) -> Path:
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def load_jobs(jobs_file: str | Path, project_dir: str | Path) -> list[Job]:
    """Read the multi-file YAML.

    Problems with a single entry are stored in ``Job.error`` so that the other
    files can still be processed; problems with the YAML as a whole raise
    ConfigError.
    """
    jobs_file = Path(jobs_file)
    try:
        data = yaml.safe_load(jobs_file.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {jobs_file}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{jobs_file} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{jobs_file}: the root must be a mapping with a 'files' list")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"{jobs_file}: unknown top-level key(s) {sorted(unknown)}. Allowed: {sorted(_TOP_KEYS)}")

    base = _resolve(Path(project_dir), data.get("base_dir", "."))
    output_dir = _resolve(base, data.get("output_dir", "files/checksum_files"))
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError(f"{jobs_file}: 'defaults' must be a mapping")
    bad_defaults = set(defaults) & _JOB_KEYS
    if bad_defaults:
        raise ConfigError(f"{jobs_file}: {sorted(bad_defaults)} must be set per file, not in 'defaults'")
    entries = data.get("files")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{jobs_file}: 'files' must be a non-empty list")

    jobs: list[Job] = []
    seen_names: dict[str, int] = {}
    seen_outputs: dict[Path, int] = {}
    for number, entry in enumerate(entries, start=1):
        job = Job(number=number, name=f"files[{number}]", input=None, output=None, enabled=True, config=None)
        jobs.append(job)
        try:
            if not isinstance(entry, dict):
                raise ConfigError("entry must be a mapping")
            if not entry.get("input"):
                raise ConfigError("'input' (raw CSV path) is required")
            job.input = _resolve(base, entry["input"])
            job.name = str(entry.get("name") or job.input.stem)
            job.enabled = bool(entry.get("enabled", True))
            job.output = (
                _resolve(base, entry["output"])
                if entry.get("output")
                else output_dir / f"{job.input.stem}_checksum.csv"
            )

            key = job.name.casefold()
            if key in seen_names:
                raise ConfigError(f"name '{job.name}' is already used by entry #{seen_names[key]}")
            seen_names[key] = number
            if job.enabled:
                out_key = Path(str(job.output).casefold())
                if out_key in seen_outputs:
                    raise ConfigError(
                        f"output {job.output} is also written by entry #{seen_outputs[out_key]}; "
                        f"set a different 'output' for one of them"
                    )
                seen_outputs[out_key] = number
                if job.output.resolve() == job.input.resolve():
                    raise ConfigError("output must not be the same file as input")

            settings = _merge(defaults, {k: v for k, v in entry.items() if k not in _JOB_KEYS})
            job.config = build_config(settings, source=f"{jobs_file.name} > {job.name}")
        except ConfigError as exc:
            job.error = str(exc)
    return jobs


def find_job(jobs: list[Job], name: str) -> Job:
    """Find an entry by its name (or input file name)."""
    wanted = name.casefold()
    for job in jobs:
        if job.name.casefold() == wanted or (job.input and job.input.name.casefold() == wanted):
            if job.error:
                raise ConfigError(f"Entry {job.label} has a config error: {job.error}")
            return job
    raise ConfigError(f"No entry named '{name}'. Names: {[j.name for j in jobs]}")
