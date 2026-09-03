# Who is selling

Moved verbatim from `CLAUDE.md` (lines 1356–1436 at commit 8697ff7,
2026-09-03): the long form of the one-line rules `CLAUDE.md` keeps. Nothing
below was rewritten. "Above", "below" and "this file" refer to the order the
text had in `CLAUDE.md`; `docs/rules/README.md` lists the files in that order.

**Who is selling is a four-state verdict too, and most of it was already in
the table** (`services/advertiser.py`). The owner asked to see the listings
sold by their owners rather than through an agency, from the list. Idealista
answers that for free: the alert email's link carries its own word for the kind
of advert -- `utm_campaign=express_newAd_sale_particular` against
`..._sale_professional` -- and nothing strips the query string, so 408 of the
730 rows answer with no request, no key and no cost. That is why the reading is
*derived* rather than stored, the same decision `utils/listing_source.py` and
`utils/municipality_grouping.py` record: a derived value cannot drift out of
agreement with the URL it came from, and a stored one would have to be written
by every future ingest path. A fotocasa import records what the page said
(`publisher.type`, with `agency.type` as the fallback and `clientTypeId` kept
as evidence) on the way past, since it has the page open anyway.

The remaining 322 rows are the hand-imported batches, and **169 of them cannot
be answered from this machine at all**: they are idealista.com links with no
campaign token, and idealista answers `403` with a DataDome captcha to every
request from here (re-measured 2026-08-17). So the fourth state is
`unchecked` -- nobody looked -- and it is never folded into `agency` because
agencies are the common case. The list badges `owner` only, for the reason the
source and sea-view badges give: a badge on 294 rows marks nothing. The
disclosure lives in the seller dropdown, which carries a count for all four
states, and on the property page, which names the evidence row by row.

Two things about it are load bearing. `read_verdict` and `state_expression` are
one answer in two languages -- the badge reads Python, the dropdown counts read
SQL, and `tests/test_advertiser.py` runs one matrix through both, because a
count that disagrees with the badges under it is a third wrong number rather
than a disclosure. And the campaign token is matched with `ESCAPE`, since every
token is full of `_`, which LIKE reads as "any character" (the lesson
`utils/listing_search.py` already records).

**And the owner can set it by hand, because for 268 rows nothing else ever
will.** Both production runs are in: every listing that arrived by alert email
is answered, and what is left is the hand-imported idealista links this machine
is refused by. So the badge on `/properties/<id>` is also the control -- a
dropdown recording `owner` or `agency`, next to what the app currently believes,
because the person with the page open in their own browser is the only reader
left. `set_by_hand` in `services/advertiser.py` is its one writer, and clearing
restores the reading the hand-set verdict displaced rather than deleting the
key: on a fotocasa row that reading cost a fetch and a 30 s wait, and a second
hand-set press keeps the *computed* one underneath rather than the first press.
`unknown` is deliberately not offered -- somebody who looked and cannot tell
leaves the row alone, and a hand-set silence would only overwrite a computed
answer. What a hand-set verdict does *not* need is a precedence branch in
`read_verdict`: it is stored under the same key as a computed reading, so the
branch that returns a measured state already returns it, and an earlier version
carrying one was dead code that stayed green when it was removed. Where it
really outranks something is on the write side, and `enrich` refuses a hand-set
row before it fetches anything.

`utils/backfill_advertiser.py` reads the pages of the rows nothing else can
answer. Free, and paced at 30 s rather than the import's courtesy 3: measured
2026-08-17, fotocasa began serving its "SENTIMOS LA INTERRUPCIÓN" page with a
`200` status after 5 requests spaced 3 s apart, and kept doing so for several
minutes. A run that collects three host refusals in a row stops instead of
walking the rest of the scope into the same wall; nothing is written for a
refusal, and the scope is "no established seller", so stopping early costs
nothing and the next run resumes. The per-listing path is the Enrich button,
which refuses the fetch outright when the row already answers for itself.

**"verified against Idealista" is gone from the UI**, because it was a
hardcoded string rendered for every row whatever site it was on, and the 56
fotocasa rows are not on Idealista at all. The per-row note names the row's own
source (`utils/listing_source.py` decides which, from the URL, and the badge,
the filter and the counts all read that one function so they cannot disagree);
the coverage line above the table says "on the source site", because the rows
it counts may come from several.

And `utils/repair_import_status_source.py` takes back the claim on the rows the
out-of-band importer left (STATUS-002 in #265). Its condition is **narrower
than the defect** and that narrowness is its whole safety: `manual` is also
what the owner's status button writes, so it repairs only rows that *also*
carry a `source_email_id` beginning `manual:`, the prefix only the importer
writes. The corroboration is that a real check stamps `listing_last_checked`
and not one of the 324 rows had one. Production was repaired on 2026-08-17 --
324 rows, snapshot in `data/status_source_manual_snapshot_20260817.json` -- so
the script finds nothing there now; it exists because the importer that
produced those rows is unchanged, and a repair that can recur should have
tests, a snapshot and a tested `restore` rather than being improvised again.
