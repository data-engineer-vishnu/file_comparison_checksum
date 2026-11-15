"""Value normalizers.

Each column that takes part in a checksum is passed through a normalizer so
that the *same logical value* produces the *same text* regardless of whether
it was exported from MSSQL (on-prem) or BigQuery (cloud).

Examples of differences these normalizers absorb:
  * ``129690.0`` vs ``129690``             -> decimal / integer
  * ``2023-06-04`` vs ``6/4/2023``          -> date
  * ``1`` / ``0`` (bit) vs ``true``/``false`` (BOOL) -> boolean
  * ``2023-06-04 10:00:00.000`` vs ``2023-06-04 10:00:00 UTC`` -> datetime
"""

from __future__ import annotations

import unicodedata
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation


class NormalizationError(ValueError):
    """Raised when a value cannot be parsed into the configured type."""


class ConfigError(ValueError):
    """Raised for invalid configuration."""


_ROUNDING = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "truncate": ROUND_DOWN,
}

_DEFAULT_TRUE = ["1", "true", "t", "yes", "y"]
_DEFAULT_FALSE = ["0", "false", "f", "no", "n"]

# Formats tried (in order) before falling back to datetime.fromisoformat.
_DEFAULT_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"]
_DEFAULT_DATETIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
]


class Normalizer:
    type_name = "base"
    allowed_options: frozenset[str] = frozenset()

    def __init__(self, column: str, options: dict):
        unknown = set(options) - self.allowed_options - {"type"}
        unknown = {k for k in unknown if not k.startswith("_")}
        if unknown:
            raise ConfigError(
                f"Column '{column}' (type '{self.type_name}'): unknown option(s) "
                f"{sorted(unknown)}. Allowed: {sorted(self.allowed_options)}"
            )
        self.column = column

    def normalize(self, value: str) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class StringNormalizer(Normalizer):
    type_name = "string"
    allowed_options = frozenset({"trim", "case", "collapse_whitespace", "unicode_form"})

    def __init__(self, column: str, options: dict):
        super().__init__(column, options)
        self.trim = bool(options.get("trim", True))
        self.case = options.get("case")
        if self.case not in (None, "upper", "lower"):
            raise ConfigError(f"Column '{column}': case must be 'upper', 'lower' or null")
        self.collapse_whitespace = bool(options.get("collapse_whitespace", False))
        self.unicode_form = options.get("unicode_form", "NFC")
        if self.unicode_form not in (None, "NFC", "NFD", "NFKC", "NFKD"):
            raise ConfigError(f"Column '{column}': invalid unicode_form '{self.unicode_form}'")

    def normalize(self, value: str) -> str:
        if self.unicode_form:
            value = unicodedata.normalize(self.unicode_form, value)
        if self.trim:
            value = value.strip()
        if self.collapse_whitespace:
            value = " ".join(value.split())
        if self.case == "upper":
            value = value.upper()
        elif self.case == "lower":
            value = value.lower()
        return value


def _to_decimal(column: str, value: str) -> Decimal:
    text = value.strip()
    try:
        d = Decimal(text)
    except InvalidOperation as exc:
        raise NormalizationError(f"'{value}' is not numeric") from exc
    if not d.is_finite():
        raise NormalizationError(f"'{value}' is not a finite number")
    return d


def _plain(d: Decimal) -> str:
    """Render a Decimal without exponent and without '-0'."""
    text = format(d, "f")
    if text.startswith("-") and Decimal(text) == 0:
        text = text[1:]
    return text


class IntegerNormalizer(Normalizer):
    type_name = "integer"

    def normalize(self, value: str) -> str:
        d = _to_decimal(self.column, value)
        if d != d.to_integral_value():
            raise NormalizationError(f"'{value}' is not an integer")
        return str(int(d))


class DecimalNormalizer(Normalizer):
    """Numeric values (DECIMAL/NUMERIC/MONEY/FLOAT/FLOAT64).

    ``scale``: number of decimal places to round to. Strongly recommended for
    FLOAT columns where the two engines print different binary approximations.
    Without ``scale`` trailing zeros are removed (``100.50`` -> ``100.5``).
    """

    type_name = "decimal"
    allowed_options = frozenset({"scale", "rounding"})

    def __init__(self, column: str, options: dict):
        super().__init__(column, options)
        self.scale = options.get("scale")
        if self.scale is not None and (not isinstance(self.scale, int) or self.scale < 0):
            raise ConfigError(f"Column '{column}': scale must be a non-negative integer")
        rounding = options.get("rounding", "half_up")
        if rounding not in _ROUNDING:
            raise ConfigError(f"Column '{column}': rounding must be one of {sorted(_ROUNDING)}")
        self.rounding = rounding

    def normalize(self, value: str) -> str:
        d = _to_decimal(self.column, value)
        if self.scale is not None:
            d = d.quantize(Decimal(1).scaleb(-self.scale), rounding=_ROUNDING[self.rounding])
        else:
            d = d.normalize()
        return _plain(d)


class BooleanNormalizer(Normalizer):
    """MSSQL BIT (1/0) vs BigQuery BOOL (true/false)."""

    type_name = "boolean"
    allowed_options = frozenset({"true_values", "false_values"})

    def __init__(self, column: str, options: dict):
        super().__init__(column, options)
        self.true_values = {str(v).strip().lower() for v in options.get("true_values", _DEFAULT_TRUE)}
        self.false_values = {str(v).strip().lower() for v in options.get("false_values", _DEFAULT_FALSE)}
        overlap = self.true_values & self.false_values
        if overlap:
            raise ConfigError(f"Column '{column}': values {sorted(overlap)} are both true and false")

    def normalize(self, value: str) -> str:
        v = value.strip().lower()
        if v in self.true_values:
            return "true"
        if v in self.false_values:
            return "false"
        raise NormalizationError(f"'{value}' is not a recognised boolean")


def _parse_datetime(value: str, formats: list[str]) -> datetime:
    text = value.strip()
    if text.upper().endswith(" UTC"):  # BigQuery TIMESTAMP export style
        text = text[:-4].strip() + "+00:00"
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace(" +", "+"))
    except ValueError as exc:
        raise NormalizationError(f"'{value}' does not match any configured format") from exc


class DateNormalizer(Normalizer):
    """DATE columns. Output is always ``output_format`` (default ISO ``%Y-%m-%d``).

    A datetime value (e.g. MSSQL ``2023-06-04 00:00:00.000``) is accepted and
    reduced to its date part. Formats are tried in order - put the unambiguous
    ones first and never list both ``%m/%d/%Y`` and ``%d/%m/%Y``.
    """

    type_name = "date"
    allowed_options = frozenset({"input_formats", "output_format"})

    def __init__(self, column: str, options: dict):
        super().__init__(column, options)
        self.input_formats = list(options.get("input_formats", _DEFAULT_DATE_FORMATS))
        self.output_format = options.get("output_format", "%Y-%m-%d")

    def normalize(self, value: str) -> str:
        parsed = _parse_datetime(value, self.input_formats + _DEFAULT_DATETIME_FORMATS)
        d: date = parsed.date()
        return d.strftime(self.output_format)


_PRECISIONS = {"seconds": 0, "milliseconds": 3, "microseconds": 6}


class DatetimeNormalizer(Normalizer):
    """DATETIME / DATETIME2 / DATETIMEOFFSET (MSSQL) vs DATETIME / TIMESTAMP (BigQuery).

    * timezone-aware values are converted to UTC, then the zone is dropped
    * ``precision`` truncates fractional seconds (MSSQL DATETIME only has
      ~3ms precision, BigQuery has microseconds) - ``seconds`` is often the
      safest choice
    """

    type_name = "datetime"
    allowed_options = frozenset({"input_formats", "precision"})

    def __init__(self, column: str, options: dict):
        super().__init__(column, options)
        self.input_formats = list(options.get("input_formats", [])) + _DEFAULT_DATETIME_FORMATS
        self.precision = options.get("precision", "microseconds")
        if self.precision not in _PRECISIONS:
            raise ConfigError(f"Column '{column}': precision must be one of {sorted(_PRECISIONS)}")

    def normalize(self, value: str) -> str:
        dt = _parse_datetime(value, self.input_formats)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        digits = _PRECISIONS[self.precision]
        base = dt.strftime("%Y-%m-%d %H:%M:%S")
        if digits == 0:
            return base
        frac = f"{dt.microsecond:06d}"[:digits]
        return f"{base}.{frac}"


NORMALIZERS: dict[str, type[Normalizer]] = {
    cls.type_name: cls
    for cls in (
        StringNormalizer,
        IntegerNormalizer,
        DecimalNormalizer,
        BooleanNormalizer,
        DateNormalizer,
        DatetimeNormalizer,
    )
}


def build_normalizer(column: str, options: dict | None) -> Normalizer:
    options = dict(options or {})
    type_name = options.get("type", "string")
    if type_name not in NORMALIZERS:
        raise ConfigError(
            f"Column '{column}': unknown type '{type_name}'. Supported: {sorted(NORMALIZERS)}"
        )
    return NORMALIZERS[type_name](column, options)
