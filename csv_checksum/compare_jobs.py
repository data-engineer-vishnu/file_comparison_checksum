"""Compare YAML: which on-prem checksum file is compared with which cloud one.

    source_dir: files/checksum_files/onprem      # folder with the on-prem checksum files
    target_dir: files/checksum_files/cloud       # folder with the cloud checksum files
    result_dir: files/comparison_results         # where result files are written
    source_label: onprem
    target_label: cloud
    only_differences: false
    auto_match_same_names: true                  # also pair files with identical names
    comparisons:
      - name: employee_records                   # optional, default = source file name without .csv
        source: employee_records_checksum.csv    # relative to source_dir, or an absolute path
        target: employee_records_checksum.csv    # relative to target_dir, or an absolute path
        result: employee_records_comparison.csv  # optional, relative to result_dir
        enabled: true                            # optional

Relative directories are resolved against ``base_dir`` (the project folder
unless the YAML sets ``base_dir``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .jobs import _resolve
from .normalizers import ConfigError

_TOP_KEYS = {
    "base_dir",
    "source_dir",
    "target_dir",
    "result_dir",
    "source_label",
    "target_label",
    "only_differences",
    "auto_match_same_names",
    "comparisons",
}
_ENTRY_KEYS = {"name", "source", "target", "result", "enabled"}


@dataclass
class ComparePair:
    number: int  # 1-based position (auto-matched pairs come after the listed ones)
    name: str
    source: Path | None
    target: Path | None
    result: Path | None
    enabled: bool = True
    auto_matched: bool = False
    error: str | None = None


@dataclass
class ComparePlan:
    source_label: str
    target_label: str
    only_differences: bool
    pairs: list[ComparePair]
    source_only: list[Path] = field(default_factory=list)  # files with no partner (auto-match)
    target_only: list[Path] = field(default_factory=list)


def _label(value, default: str) -> str:
    text = str(value if value is not None else default).strip()
    if not text or not text.replace("_", "").isalnum():
        raise ConfigError(f"label '{text}' must be letters, digits or '_' (it is used in column names)")
    return text


def load_compare_plan(config_file: str | Path, project_dir: str | Path) -> ComparePlan:
    config_file = Path(config_file)
    try:
        data = yaml.safe_load(config_file.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_file}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_file} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{config_file}: the root must be a mapping")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"{config_file}: unknown top-level key(s) {sorted(unknown)}. Allowed: {sorted(_TOP_KEYS)}")

    base = _resolve(Path(project_dir), data.get("base_dir", "."))
    for key in ("source_dir", "target_dir"):
        if not data.get(key):
            raise ConfigError(f"{config_file}: '{key}' is required")
    source_dir = _resolve(base, data["source_dir"])
    target_dir = _resolve(base, data["target_dir"])
    result_dir = _resolve(base, data.get("result_dir", "files/comparison_results"))
    source_label = _label(data.get("source_label"), "onprem")
    target_label = _label(data.get("target_label"), "cloud")
    if source_label.casefold() == target_label.casefold():
        raise ConfigError(f"{config_file}: source_label and target_label must be different")

    entries = data.get("comparisons") or []
    if not isinstance(entries, list):
        raise ConfigError(f"{config_file}: 'comparisons' must be a list")
    auto = bool(data.get("auto_match_same_names", False))
    if not entries and not auto:
        raise ConfigError(f"{config_file}: list files under 'comparisons' or set auto_match_same_names: true")

    pairs: list[ComparePair] = []
    for number, entry in enumerate(entries, start=1):
        pair = ComparePair(number=number, name=f"comparisons[{number}]", source=None, target=None, result=None)
        pairs.append(pair)
        try:
            if not isinstance(entry, dict):
                raise ConfigError("entry must be a mapping")
            bad = {k for k in entry if k not in _ENTRY_KEYS}
            if bad:
                raise ConfigError(f"unknown key(s) {sorted(bad)}. Allowed: {sorted(_ENTRY_KEYS)}")
            for key in ("source", "target"):
                if not entry.get(key):
                    raise ConfigError(f"'{key}' is required")
            pair.source = _resolve(source_dir, entry["source"])
            pair.target = _resolve(target_dir, entry["target"])
            pair.name = str(entry.get("name") or pair.source.stem)
            pair.enabled = bool(entry.get("enabled", True))
        except ConfigError as exc:
            pair.error = str(exc)
        if pair.error is None:
            pair.result = (
                _resolve(result_dir, entry["result"]) if entry.get("result")
                else result_dir / f"{pair.name}_comparison.csv"
            )

    source_only: list[Path] = []
    target_only: list[Path] = []
    if auto:
        for d, what in ((source_dir, "source_dir"), (target_dir, "target_dir")):
            if not d.is_dir():
                raise ConfigError(f"{config_file}: {what} {d} does not exist")
        used_src = {p.source.resolve() for p in pairs if p.source}
        used_tgt = {p.target.resolve() for p in pairs if p.target}
        src_files = {f.name.casefold(): f for f in sorted(source_dir.glob("*.csv")) if f.resolve() not in used_src}
        tgt_files = {f.name.casefold(): f for f in sorted(target_dir.glob("*.csv")) if f.resolve() not in used_tgt}
        for key, src in src_files.items():
            tgt = tgt_files.get(key)
            if tgt is None:
                source_only.append(src)
                continue
            pairs.append(ComparePair(
                number=len(pairs) + 1, name=src.stem, source=src, target=tgt,
                result=result_dir / f"{src.stem}_comparison.csv", auto_matched=True,
            ))
        target_only = [f for k, f in tgt_files.items() if k not in src_files]

    seen_names: dict[str, int] = {}
    seen_results: dict[str, int] = {}
    for pair in pairs:
        if pair.error:
            continue
        key = pair.name.casefold()
        if key in seen_names:
            pair.error = f"name '{pair.name}' is already used by entry #{seen_names[key]}"
            continue
        seen_names[key] = pair.number
        if pair.enabled:
            rkey = str(pair.result).casefold()
            if rkey in seen_results:
                pair.error = f"result {pair.result} is also written by entry #{seen_results[rkey]}"
                continue
            seen_results[rkey] = pair.number

    return ComparePlan(
        source_label=source_label,
        target_label=target_label,
        only_differences=bool(data.get("only_differences", False)),
        pairs=pairs,
        source_only=source_only,
        target_only=target_only,
    )
