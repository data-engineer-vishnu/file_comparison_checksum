"""Raw CSV -> checksum CSV.

The checksum file contains ONLY the primary key column(s) and the row checksum:

    <pk_1>, ..., <pk_n>, checksum

* one row per distinct primary key
* if a key occurs more than once in the raw file, its row checksums are
  combined (order-independently) into one checksum and a warning is printed;
  the combined checksum never equals a single-row checksum, so a duplicate on
  only one side shows up as a MISMATCH
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .normalizers import ConfigError, NormalizationError

CHECKSUM_COL = "checksum"

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


@dataclass
class ColumnLayout:
    header: list[str]
    pk_indexes: list[int]
    pk_names: list[str]  # as spelled in the config
    hashed_indexes: list[int]
    hashed_names: list[str]  # as spelled in the file header
    ignored_names: list[str]  # present in the file but neither PK nor hashed
    excluded_names: list[str]


def resolve_layout(config: Config, header: list[str]) -> ColumnLayout:
    keys = [config.key(h) for h in header]
    dupes = [h for h, n in Counter(keys).items() if n > 1]
    if dupes:
        raise ConfigError(f"Input header has duplicate column names: {dupes}")
    index = {k: i for i, k in enumerate(keys)}

    def require(cols: list[str], what: str) -> None:
        missing = [c for c in cols if config.key(c) not in index]
        if missing:
            raise ConfigError(
                f"{what} not found in the input header: {missing}. Header is: {header}"
            )

    require(config.primary_keys, "Primary key column(s)")
    # Excluded columns that are absent from the file are fine - nothing to exclude.
    if config.include_columns is not None:
        require(config.include_columns, "include_columns")

    pk_keys = {config.key(c) for c in config.primary_keys}
    excluded = {config.key(c) for c in config.exclude_columns}
    overlap = pk_keys & excluded
    if overlap:
        raise ConfigError(f"Primary key column(s) cannot be excluded: {sorted(overlap)}")
    if CHECKSUM_COL in {c.strip().lower() for c in config.primary_keys}:
        raise ConfigError(f"A primary key column cannot be named '{CHECKSUM_COL}'")

    candidates = keys if config.include_columns is None else [config.key(c) for c in config.include_columns]
    # Sorted so that column order in the file never affects the checksum.
    hashed = sorted({k for k in candidates if k not in pk_keys and k not in excluded})
    if not hashed:
        raise ConfigError("No columns left to checksum after applying include/exclude lists")

    used = pk_keys | set(hashed)
    return ColumnLayout(
        header=header,
        pk_indexes=[index[config.key(c)] for c in config.primary_keys],
        pk_names=list(config.primary_keys),
        hashed_indexes=[index[k] for k in hashed],
        hashed_names=[header[index[k]] for k in hashed],
        ignored_names=[h for h, k in zip(header, keys) if k not in used and k not in excluded],
        excluded_names=[h for h, k in zip(header, keys) if k in excluded],
    )


@dataclass
class Stats:
    input_rows: int = 0
    blank_lines_skipped: int = 0
    ragged_rows: int = 0
    ragged_row_lines: list[int] = field(default_factory=list)
    null_counts: Counter = field(default_factory=Counter)
    parse_errors: Counter = field(default_factory=Counter)
    parse_error_lines: dict = field(default_factory=dict)  # column -> first few line numbers


class RowNormalizer:
    """Turns a raw CSV row into normalized PK values and hashed values."""

    def __init__(self, config: Config, layout: ColumnLayout, stats: Stats):
        self.config = config
        self.stats = stats
        h = layout.header
        self.pk = [(i, h[i], config.normalizer_for(h[i])) for i in layout.pk_indexes]
        self.hashed = [(i, h[i], config.normalizer_for(h[i])) for i in layout.hashed_indexes]

    def _value(self, row: list[str], idx: int, name: str, normalizer, line_no: int) -> str:
        raw = row[idx] if idx < len(row) else None
        if self.config.is_null(raw):
            self.stats.null_counts[name] += 1
            return self.config.null_token
        try:
            return normalizer.normalize(raw)
        except NormalizationError as exc:
            if self.config.on_parse_error == "error":
                raise NormalizationError(f"Line {line_no}, column '{name}': {exc}") from exc
            self.stats.parse_errors[name] += 1
            lines = self.stats.parse_error_lines.setdefault(name, [])
            if len(lines) < 10:
                lines.append(line_no)
            return raw.strip()

    def pk_values(self, row: list[str], line_no: int) -> tuple[str, ...]:
        return tuple(self._value(row, i, n, f, line_no) for i, n, f in self.pk)

    def hashed_values(self, row: list[str], line_no: int) -> list[str]:
        return [self._value(row, i, n, f, line_no) for i, n, f in self.hashed]


def iter_rows(config: Config, input_path: Path, stats: Stats):
    """Yield (line_no, header, row). Header is yielded first with row=None."""
    if not input_path.exists():
        raise ConfigError(f"Input file not found: {input_path}")
    with open(input_path, newline="", encoding=config.encoding) as fh:
        reader = csv.reader(fh, delimiter=config.delimiter, quotechar=config.quotechar)
        try:
            header = next(reader)
        except StopIteration:
            raise ConfigError(f"Input file {input_path} is empty") from None
        yield 1, header, None
        for row in reader:
            line_no = reader.line_num
            if not row or (len(row) == 1 and row[0].strip() == "" and len(header) > 1):
                stats.blank_lines_skipped += 1
                continue
            if len(row) != len(header):
                stats.ragged_rows += 1
                if len(stats.ragged_row_lines) < 10:
                    stats.ragged_row_lines.append(line_no)
            yield line_no, header, row


def _hash(algorithm: str, text: str) -> str:
    return hashlib.new(algorithm, text.encode("utf-8")).hexdigest()


def _row_payload(values: list[str]) -> str:
    # JSON encoding of the ordered value list is unambiguous: no separator
    # character inside a value can make two different rows hash the same.
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


@dataclass
class GenerateResult:
    output_path: Path
    input_rows: int
    distinct_keys: int
    duplicate_keys: int
    stats: Stats
    layout: ColumnLayout


def generate(config: Config, input_path: Path, output_path: Path) -> GenerateResult:
    stats = Stats()
    rows_iter = iter_rows(config, input_path, stats)
    _, header, _ = next(rows_iter)
    layout = resolve_layout(config, header)
    normalizer = RowNormalizer(config, layout, stats)
    algo = config.hash_algorithm

    entries: dict[tuple[str, ...], list[str]] = {}  # pk -> row checksums
    for line_no, _, row in rows_iter:
        stats.input_rows += 1
        pk = normalizer.pk_values(row, line_no)
        checksum = _hash(algo, _row_payload(normalizer.hashed_values(row, line_no)))
        entries.setdefault(pk, []).append(checksum)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    duplicate_keys = 0
    # Write to a temp file and swap it in only when complete, so a failed run
    # never leaves a half-written checksum file behind.
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    with open(tmp_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(layout.pk_names + [CHECKSUM_COL])
        for pk, checksums in entries.items():
            if len(checksums) == 1:
                checksum = checksums[0]
            else:
                duplicate_keys += 1
                checksum = _hash(algo, f"DUP:{len(checksums)}:" + "|".join(sorted(checksums)))
            writer.writerow(list(pk) + [checksum])
    os.replace(tmp_path, output_path)

    return GenerateResult(
        output_path=output_path,
        input_rows=stats.input_rows,
        distinct_keys=len(entries),
        duplicate_keys=duplicate_keys,
        stats=stats,
        layout=layout,
    )


def inspect(
    config: Config, input_path: Path, keys: set[tuple[str, ...]], output_path: Path
) -> int:
    """Write normalized values of the given primary keys (LOCAL USE ONLY - contains data)."""
    stats = Stats()
    rows_iter = iter_rows(config, input_path, stats)
    _, header, _ = next(rows_iter)
    layout = resolve_layout(config, header)
    normalizer = RowNormalizer(config, layout, stats)
    found = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["line_no"] + layout.pk_names + layout.hashed_names)
        for line_no, _, row in rows_iter:
            pk = normalizer.pk_values(row, line_no)
            if pk in keys:
                found += 1
                writer.writerow([line_no] + list(pk) + normalizer.hashed_values(row, line_no))
    return found
