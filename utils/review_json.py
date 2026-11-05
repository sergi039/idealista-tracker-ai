"""Browser-compatible frozen review fingerprints shared by server/importer."""

from decimal import Decimal
from datetime import datetime
import json
import math
import re


class InvalidFeedback(ValueError):
    pass


def valid_timestamp(value):
    """ISO timestamps accepted by the browser, without normalized offset minutes."""
    if value is None:
        return True
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-5][0-9]:[0-5][0-9]"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
        value,
    ):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def valid_reason(value):
    """The same 2000 UTF-16-unit limit the browser's textarea enforces."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
        return len(value.encode("utf-16-le")) <= 4000
    except UnicodeError:
        return False


def javascript_number(value):
    """Render finite binary64 numbers with JSON.stringify's decimal thresholds.

    Python's shortest round-trip representation supplies the significant digits;
    JavaScript uses fixed notation from 1e-6 through values below 1e21. Converting
    large Python integers first also mirrors JSON.parse's binary64 rounding.
    """
    if type(value) is int and abs(value) <= 2**53:
        return str(value)
    try:
        number = float(value)
    except OverflowError as exc:
        raise InvalidFeedback("Non-finite JSON number") from exc
    if not math.isfinite(number):
        raise InvalidFeedback("Non-finite JSON number")
    if number == 0:
        return "0"
    sign, significant, exponent = Decimal(repr(number)).as_tuple()
    digits = "".join(str(digit) for digit in significant)
    trimmed = digits.rstrip("0")
    exponent += len(digits) - len(trimmed)
    digits = trimmed
    point = len(digits) + exponent
    if 0 < point <= 21:
        rendered = (
            digits + "0" * (point - len(digits))
            if point >= len(digits)
            else digits[:point] + "." + digits[point:]
        )
    elif -6 < point <= 0:
        rendered = "0." + "0" * -point + digits
    else:
        tail = "." + digits[1:] if len(digits) > 1 else ""
        power = point - 1
        rendered = digits[0] + tail + "e" + ("+" if power >= 0 else "") + str(power)
    return ("-" if sign else "") + rendered


def canonical(value):
    """Match the artifact's sorted JSON.stringify for its finite JSON numbers."""
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(canonical(k) + ":" + canonical(value[k]) for k in sorted(value))
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if type(value) in (int, float):
        return javascript_number(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
