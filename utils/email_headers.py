"""Reading MIME headers safely.

A header longer than the line limit arrives *folded* (RFC 5322 2.2.3): the
value continues on the next line after a CRLF plus at least one whitespace
character. Unfolding drops the line break and keeps the whitespace. It is not
cosmetic cleanup -- a folded value that reaches a parser as-is carries a CR in
the middle of a word, and every regex with a `[^\\r\\n]` character class
truncates silently at that point.

Idealista alert subjects are long enough to fold, and the fold lands in a
different spot depending on the subject prefix ("New detached house" versus
"Price reduction"). That is how a single saved search ended up spread across
four SearchProfile rows.
"""

import re
from email.header import decode_header

# A line break that is followed by continuation whitespace: the break goes,
# the whitespace stays, because it is part of the value.
_FOLD = re.compile(r"\r?\n(?=[ \t])")

# A line break with no continuation whitespace cannot legally appear inside a
# header value. Treat it as transport noise and keep the value on one line, so
# that no downstream parser can truncate on it.
_STRAY_BREAK = re.compile(r"\r\n?|\n")


def unfold_header(value) -> str:
    """Return a folded header value joined back into one logical line."""
    if value is None:
        return ""
    return _STRAY_BREAK.sub(" ", _FOLD.sub("", str(value))).strip()


def decode_header_value(value) -> str:
    """Unfold a header, then decode its RFC 2047 encoded-words to text.

    Falls back to the unfolded value if decoding fails: a header we cannot
    decode is still more useful unfolded than folded.
    """
    if value is None:
        return ""

    unfolded = unfold_header(value)
    try:
        parts = []
        for part, encoding in decode_header(unfolded):
            if isinstance(part, bytes):
                parts.append(part.decode(encoding or "utf-8", errors="ignore"))
            else:
                parts.append(part)
        return " ".join(parts)
    except Exception:
        return unfolded
