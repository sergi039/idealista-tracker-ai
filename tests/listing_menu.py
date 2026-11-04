"""Read subscription menu counts against the applied selection on the page.

The approved workspace replaces chip links with checkbox + Apply. The map
representation link carries the same applied query; selecting one subscription
changes only profile_id. Form ownership and draft behavior are tested separately.
"""

import re
from html import unescape
from urllib.parse import parse_qs, urlencode, urlparse


def subscription_options(body):
    map_link = re.search(r'<a\b[^>]*id="view-map-btn"[^>]*href="([^"]+)"', body)
    if not map_link:
        map_link = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*id="view-map-btn"', body)
    assert map_link, "the applied selection has no map representation link"
    state = parse_qs(urlparse(unescape(map_link.group(1))).query)
    options = {}
    for pid, label in re.findall(
        r'<label\b[^>]*for="profile-option-([^" ]+)"[^>]*>(.*?)</label>', body, re.S
    ):
        match = re.match(
            r'\s*([^<]+)\s*<span class="text-body-secondary">\((\d+)\)</span>', label
        )
        assert match, "subscription label must disclose its filtered count"
        params = dict(state, profile_id=[pid], page=["1"])
        options[unescape(match.group(1)).strip()] = (
            int(match.group(2)),
            "/properties?" + urlencode(params, doseq=True),
        )
    return options
