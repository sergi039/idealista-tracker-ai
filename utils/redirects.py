"""Redirect-target validation.

Lives here rather than in `utils/auth.py` because that module was removed
along with the admin login (2026-08-08). The open-redirect guard below is
independent of authentication: the Referer header is client-controlled
whether or not anyone is logged in.
"""

from flask import request


def safe_referrer_redirect(fallback):
    """Return `request.referrer` only when it is same-origin, else `fallback`.

    Open-redirect guard (issue #17): several POST handlers redirect back to
    "wherever the user came from" using the Referer header, but that header
    is entirely client-controlled. A cross-origin form submission (or a
    hand-crafted request) can set it to an attacker's origin and bounce the
    browser there right after the action completes. Only honor it when it
    points back at this same host.
    """
    referrer = request.referrer
    if referrer and referrer.startswith(request.host_url):
        return referrer
    return fallback
