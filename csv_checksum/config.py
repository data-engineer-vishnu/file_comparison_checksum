"""Validation of the per-file checksum settings.

The settings for one file (primary keys, columns, rules, NULL handling, ...)
are turned into a ``Config``. The *same* columns and rules must be used
on-prem and in the cloud for a file, otherwise every row will show as a
mismatch. The multi-file YAML is read by ``jobs.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .normalizers import ConfigError, Normalizer, build_normalizer

_TOP_LEVEL_KEYS = {
    "description",
    "primary_keys",
    "include_columns",
    "exclude_columns",
    "column_rules",
    "default_rule",
    "null_handling",
    "hash_algorithm",
    "on_parse_error",
    "case_insensitive_headers",
    "csv",
}
_CSV_KEYS = {"delimiter", "quotechar", "encoding"}
_NULL_KEYS = {"null_token", "null_values", "case_insensitive"}
_SUPPORTED_HASHES = {"sha256", "sha1", "md5", "sha512", "blake2b"}


@dataclass
class Config:
    source: str  # e.g. "jobs.yaml > employee_records" (used in messages)
    primary_keys: list[str]
    include_columns: list[str] | None  # None == "*" (all columns)
    exclude_columns: list[str]
    column_rules: dict[str, Normalizer]
    default_rule_options: dict
    null_token: str
    null_values: set[str]
    null_case_insensitive: bool
    hash_algorithm: str
    on_parse_error: str
    case_insensitive_headers: bool
    delimiter: str
    quotechar: str
    encoding: str
    _default_cache: dict[str, Normalizer] = field(default_factory=dict, repr=False)

    # -- helpers -----------------------------------------------------------
    def key(self, column: str) -> str:
        """Header-matching key for a column name."""
        return column.strip().casefold() if self.case_insensitive_headers else column.strip()

    def normalizer_for(self, column: str) -> Normalizer:
        rule = self.column_rules.get(self.key(column))
        if rule is not None:
            return rule
        k = self.key(column)
        if k not in self._default_cache:
            self._default_cache[k] = build_normalizer(column, self.default_rule_options)
        return self._default_cache[k]

    def is_null(self, raw: str | None) -> bool:
        if raw is None:
            return True
        v = raw.strip()
        if self.null_case_insensitive:
            v = v.upper()
        return v in self.null_values


def _check_keys(section: str, data: dict, allowed: set[str]) -> None:
    unknown = {k for k in data if k not in allowed and not k.startswith("_")}
    if unknown:
        raise ConfigError(f"{section}: unknown key(s) {sorted(unknown)}. Allowed: {sorted(allowed)}")


def _str_list(name: str, value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"'{name}' must be a list of non-empty column names")
    return value


def build_config(data: dict, source: str) -> Config:
    """Build a Config from one file entry (already merged with the defaults)."""
    if not isinstance(data, dict):
        raise ConfigError("Config must be a YAML mapping")
    _check_keys("config", data, _TOP_LEVEL_KEYS)

    case_insensitive = bool(data.get("case_insensitive_headers", True))

    def key(c: str) -> str:
        return c.strip().casefold() if case_insensitive else c.strip()

    if "primary_keys" not in data:
        raise ConfigError("'primary_keys' is required")
    primary_keys = _str_list("primary_keys", data["primary_keys"])
    if not primary_keys:
        raise ConfigError("'primary_keys' must contain at least one column")

    include = data.get("include_columns", "*")
    include_columns = None if include == "*" else _str_list("include_columns", include)
    exclude_columns = _str_list("exclude_columns", data.get("exclude_columns") or [])

    rules_raw = data.get("column_rules") or {}
    if not isinstance(rules_raw, dict):
        raise ConfigError("'column_rules' must be a mapping of column -> rule")
    column_rules = {}
    for col, opts in rules_raw.items():
        if not isinstance(opts, dict):
            raise ConfigError(f"column_rules['{col}'] must be a mapping")
        column_rules[key(col)] = build_normalizer(col, opts)

    default_rule = data.get("default_rule", {"type": "string"})
    build_normalizer("<default_rule>", default_rule)  # validate early

    nulls = data.get("null_handling") or {}
    _check_keys("null_handling", nulls, _NULL_KEYS)
    null_ci = bool(nulls.get("case_insensitive", True))
    null_values = {
        (str(v).strip().upper() if null_ci else str(v).strip())
        for v in nulls.get("null_values", ["", "NULL"])
    }
    null_token = str(nulls.get("null_token", "<<NULL>>"))

    hash_algorithm = data.get("hash_algorithm", "sha256")
    if hash_algorithm not in _SUPPORTED_HASHES:
        raise ConfigError(f"hash_algorithm must be one of {sorted(_SUPPORTED_HASHES)}")

    on_parse_error = data.get("on_parse_error", "keep_raw")
    if on_parse_error not in ("keep_raw", "error"):
        raise ConfigError("on_parse_error must be 'keep_raw' or 'error'")

    csv_opts = data.get("csv") or {}
    _check_keys("csv", csv_opts, _CSV_KEYS)

    return Config(
        source=source,
        primary_keys=primary_keys,
        include_columns=include_columns,
        exclude_columns=exclude_columns,
        column_rules=column_rules,
        default_rule_options=default_rule,
        null_token=null_token,
        null_values=null_values,
        null_case_insensitive=null_ci,
        hash_algorithm=hash_algorithm,
        on_parse_error=on_parse_error,
        case_insensitive_headers=case_insensitive,
        delimiter=csv_opts.get("delimiter", ","),
        quotechar=csv_opts.get("quotechar", '"'),
        encoding=csv_opts.get("encoding", "utf-8-sig"),
    )
