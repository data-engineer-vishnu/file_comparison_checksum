"""Compare two checksum files (e.g. on-prem vs cloud) by primary key.

A checksum file is ``<pk_1>, ..., <pk_n>, checksum``: every column before
``checksum`` is treated as part of the primary key.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .generator import CHECKSUM_COL
from .normalizers import ConfigError

MATCH = "MATCH"
MISMATCH = "MISMATCH"


@dataclass
class ChecksumFile:
    path: Path
    pk_names: list[str]
    rows: dict[tuple[str, ...], str]  # pk -> checksum


def load_checksum_file(path: Path) -> ChecksumFile:
    if not path.exists():
        raise ConfigError(f"Checksum file not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        lowered = [h.strip().lower() for h in header or []]
        if not header or lowered[-1:] != [CHECKSUM_COL] or len(header) < 2:
            raise ConfigError(
                f"{path} is not a checksum file (expected primary key column(s) followed by a "
                f"last column named '{CHECKSUM_COL}'). Header is: {header}"
            )
        n_pk = len(header) - 1
        rows: dict = {}
        for row in reader:
            if not row:
                continue
            pk = tuple(row[:n_pk])
            if pk in rows:
                raise ConfigError(f"{path}: primary key {pk} appears more than once")
            rows[pk] = row[n_pk]
    return ChecksumFile(path=path, pk_names=header[:n_pk], rows=rows)


@dataclass
class CompareResult:
    output_path: Path
    counts: Counter
    sample_differences: list[tuple[tuple[str, ...], str]] = field(default_factory=list)

    @property
    def all_match(self) -> bool:
        return set(self.counts) <= {MATCH}


def compare(
    source_path: Path,
    target_path: Path,
    output_path: Path,
    source_label: str = "onprem",
    target_label: str = "cloud",
    only_differences: bool = False,
) -> CompareResult:
    src = load_checksum_file(source_path)
    tgt = load_checksum_file(target_path)

    if [p.casefold() for p in src.pk_names] != [p.casefold() for p in tgt.pk_names]:
        raise ConfigError(
            f"Primary keys differ: {source_label}={src.pk_names} {target_label}={tgt.pk_names}"
        )

    s, t = source_label, target_label
    missing_in_t = f"MISSING_IN_{t.upper()}"
    missing_in_s = f"MISSING_IN_{s.upper()}"

    counts: Counter = Counter()
    samples: list = []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(src.pk_names + ["status", f"{s}_checksum", f"{t}_checksum"])

        def emit(pk, status, a, b):
            counts[status] += 1
            if status != MATCH and len(samples) < 20:
                samples.append((pk, status))
            if not (only_differences and status == MATCH):
                writer.writerow(list(pk) + [status, a or "", b or ""])

        for pk, a in src.rows.items():
            b = tgt.rows.get(pk)
            if b is None:
                emit(pk, missing_in_t, a, None)
            else:
                emit(pk, MATCH if a == b else MISMATCH, a, b)
        for pk, b in tgt.rows.items():
            if pk not in src.rows:
                emit(pk, missing_in_s, None, b)

    return CompareResult(output_path=output_path, counts=counts, sample_differences=samples)
