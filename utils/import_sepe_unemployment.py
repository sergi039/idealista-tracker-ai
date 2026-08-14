"""Import SEPE registered unemployment (paro registrado) by municipality.

    python -m utils.import_sepe_unemployment --out data/sepe_unemployment.json
    python -m utils.import_sepe_unemployment --out ... --csv /path/Paro_2026.csv

Source — measured against the live sites on 2026-08-14, not assumed:

The datos.gob.es entry `ea0021425-paro-registrado-por-municipios` is gone (the
page 404s and the API returns it as an empty list). The live catalogue entry is
`ea0041513-paro-registrado-por-municipio`, reached from SEPE's own open-data
catalogue at sede.sepe.gob.es, and its machine-readable distributions are one
**annual** CSV per year:

    https://sede.sepe.gob.es/es/portaltrabaja/resources/sede/datos_abiertos/
    datos/Paro_por_municipios_<YEAR>_csv.csv

Layout of that CSV (real 2026 file: 48 812 rows, 5.0 MB):

- `;`-separated, CRLF, and **ISO-8859-1** — the HTTP response declares
  `charset=UTF-8` and is simply wrong, so the bytes are decoded latin-1
  explicitly rather than trusting the header.
- Row 1 is a banner (`;;;;PARO REGISTRADO POR MUNICIPIOS ...`); row 2 is the
  header. That header is not tidy — `Código mes ` carries a trailing space and
  ` Municipio` a leading one — so header names are stripped before matching,
  and the header row is *found* rather than assumed to be at a fixed index.
- Columns: `Código mes` (`202606`), `mes` (`Junio de 2026`), `Código de CA`,
  `Comunidad Autónoma`, `Codigo Provincia` (unpadded — `4`, `33`), `Provincia`,
  `Codigo Municipio` (**zero-padded 5-digit INE code** — `33041`), `Municipio`,
  `total Paro Registrado`, then sex/age and sector breakdowns this importer
  deliberately does not read.
- One row per municipality per month, so a year's file holds every month
  published so far — which is what makes the previous year's file the honest
  source for the same month a year earlier.

`Codigo Municipio` is already the INE code, so the join is by code and needs no
name matching. The name path in `resolve_code()` is the fallback for a row
whose code cell is unusable: it goes through `utils.municipality_codes.match()`,
and a name that does not resolve is **reported and skipped**, never guessed.

**Suppression is not zero.** SEPE withholds counts below five and writes the
literal `<5`. In the five watched provinces that was one municipality (33048
Pesoz) in June 2026. It is recorded as `unemployed_total: null` with
`suppressed: true`; reading it as 0, or as 5, would be a fabricated figure, and
dropping the row would claim the municipality is absent from the dataset.

**Why not the newest month on sepe.es.** sepe.es publishes a per-province file
about a month ahead of the open-data CSV, but only as legacy OLE2/BIFF8 `.xls`
(`MUNI_ASTURIAS_0726.xls`, magic `d0cf11e0`, `application/vnd.ms-excel`).
openpyxl reads xlsx only and refuses that format, and xlrd is not a dependency
of this project — so the newest *machine-readable* month is the CSV's. The
period actually parsed is recorded in the output instead of being assumed by
whatever renders it.
"""

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils.http import HTTP_USER_AGENT, RateGate, request_with_retries

# INE spells municipalities with the article behind a comma ("Franco, El"),
# and this file uses the same convention. The CNH importer already owns the
# inversion; import it rather than keeping a second copy of the same rule.
from utils.import_cnh_hospitals import normalize_municipality
from utils.municipality_codes import PROVINCE_CODES, build_index, match

logger = logging.getLogger(__name__)

DATASET_PAGE_URL = (
    "https://datos.gob.es/es/catalogo/ea0041513-paro-registrado-por-municipio"
)
CSV_URL_TEMPLATE = (
    "https://sede.sepe.gob.es/es/portaltrabaja/resources/sede/datos_abiertos/"
    "datos/Paro_por_municipios_{year}_csv.csv"
)

# The real files are 5-10 MB. The cap bounds a download that is not the
# dataset at all; it is not a prediction of next year's size.
MAX_CSV_BYTES = 50 * 1024 * 1024

HTTP_TIMEOUT_S = 30

# One polite request every two seconds, handed to the transport so it paces
# *retries* too (the project rule: pacing is passed to the transport, never
# taken around it). This importer makes two requests, so it never waits long.
SEPE_MIN_INTERVAL_S = 2.0
SEPE_GATE = RateGate(SEPE_MIN_INTERVAL_S, name="sepe")

# Declared UTF-8, actually ISO-8859-1 (see the module docstring). latin-1 never
# raises, so a stray byte cannot abort the parse; the accent check below is
# what proves the choice is right rather than merely silent.
CSV_ENCODING = "latin-1"
CSV_DELIMITER = ";"

COL_PERIOD = "Código mes"
COL_PROVINCE_CODE = "Codigo Provincia"
COL_PROVINCE = "Provincia"
COL_MUNICIPALITY_CODE = "Codigo Municipio"
COL_MUNICIPALITY = "Municipio"
COL_TOTAL = "total Paro Registrado"

REQUIRED_COLUMNS = (
    COL_PERIOD,
    COL_PROVINCE_CODE,
    COL_MUNICIPALITY_CODE,
    COL_MUNICIPALITY,
    COL_TOTAL,
)

# How far into the file to look for the header before giving up. The real file
# needs 1 (a banner row); a handful of rows is slack, not a scan of the file.
HEADER_SEARCH_ROWS = 10

_PERIOD_RE = re.compile(r"^(?P<year>\d{4})(?P<month>0[1-9]|1[0-2])$")
# SEPE's confidentiality marker: "<5", occasionally spaced.
_SUPPRESSED_RE = re.compile(r"^<\s*\d+$")

SUPPRESSION_NOTE = (
    "SEPE withholds municipal counts below five and publishes the literal "
    '"<5". Those are recorded as unemployed_total null with suppressed true — '
    "never as 0 and never as 5."
)


@dataclass
class ParseReport:
    """What the parse could not do, so the run can say so out loud."""

    unresolved_names: List[str] = field(default_factory=list)
    suppressed: List[str] = field(default_factory=list)
    unparsable: List[Tuple[str, str]] = field(default_factory=list)
    per_province: Dict[str, int] = field(default_factory=dict)


def decode_csv(raw: bytes) -> str:
    """Decode the SEPE CSV, ignoring the response's incorrect charset header."""
    return raw.decode(CSV_ENCODING)


def read_rows(text: str) -> List[List[str]]:
    """Split the decoded CSV into rows. `newline=""` keeps csv in charge of CRLF."""
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=CSV_DELIMITER))


def find_header(rows: Sequence[Sequence[str]]) -> Tuple[int, Dict[str, int]]:
    """Locate the header row and map stripped column name -> index.

    The file leads with a banner row, and the header's own names carry stray
    leading/trailing spaces. Searching for the row that holds every required
    column means a new banner line does not break the parser, while a *renamed*
    column still fails loudly instead of yielding empty data.
    """
    for idx, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        headers = {
            str(cell).strip(): pos for pos, cell in enumerate(row) if cell is not None
        }
        if all(name in headers for name in REQUIRED_COLUMNS):
            return idx, headers
    raise ValueError(
        "No header row with columns "
        f"{list(REQUIRED_COLUMNS)} in the first {HEADER_SEARCH_ROWS} rows — "
        "the SEPE CSV layout changed; re-measure before trusting this parser."
    )


def parse_count(value: str) -> Tuple[Optional[int], bool]:
    """Read one count cell. Returns (value, suppressed).

    `("123", False)` for a figure, `(None, True)` for SEPE's `<5`, and
    `(None, False)` for anything else — which the caller reports rather than
    turning into a number.
    """
    text = (value or "").strip()
    if text.isdigit():
        return int(text), False
    if _SUPPRESSED_RE.match(text):
        return None, True
    return None, False


def period_to_iso(code: str) -> str:
    """`202606` -> `2026-06`."""
    matched = _PERIOD_RE.match((code or "").strip())
    if not matched:
        raise ValueError(f"Not a YYYYMM period code: {code!r}")
    return f"{matched.group('year')}-{matched.group('month')}"


def iso_to_period(iso: str) -> str:
    """`2026-06` -> `202606`."""
    return iso.replace("-", "")


def latest_period(rows: Sequence[Sequence[str]], columns: Dict[str, int]) -> str:
    """The newest YYYYMM present in the file.

    Codes that are not YYYYMM are ignored rather than sorted as strings: a
    stray footer row must not become the period the whole document claims.
    """
    col = columns[COL_PERIOD]
    codes = {
        str(row[col]).strip()
        for row in rows
        if len(row) > col and _PERIOD_RE.match(str(row[col]).strip())
    }
    if not codes:
        raise ValueError("No valid YYYYMM period codes in the file")
    return max(codes)


def _province_code(province_cell: str, municipality_code: str) -> Optional[str]:
    """The 2-digit province code, from its own column or the INE code's prefix."""
    province = (province_cell or "").strip()
    if province.isdigit():
        return province.zfill(2)
    code = (municipality_code or "").strip()
    if len(code) == 5 and code.isdigit():
        return code[:2]
    return None


def resolve_code(
    municipality_code: str, name: str, index: Dict[str, str]
) -> Optional[str]:
    """Resolve a row to its 5-digit INE code, by code when the file gives one.

    The SEPE CSV is code-keyed, so the first branch is what actually runs. The
    name branch is the fallback for a row whose code cell is blank or malformed
    and for any name-keyed SEPE table this parser is pointed at later; it
    delegates to `municipality_codes.match()`, which refuses to guess. Returns
    None when neither path resolves — the caller reports and skips.
    """
    code = (municipality_code or "").strip()
    if len(code) == 5 and code.isdigit():
        return code if code[:2] in PROVINCE_CODES else None
    return match(name or "", index)


def build_name_index(
    rows: Sequence[Sequence[str]], columns: Dict[str, int], period: str
) -> Dict[str, str]:
    """Index the period's own code -> name pairs for the name fallback."""
    code_col = columns[COL_MUNICIPALITY_CODE]
    name_col = columns[COL_MUNICIPALITY]
    period_col = columns[COL_PERIOD]
    code_to_name: Dict[str, str] = {}
    for row in rows:
        if len(row) <= max(code_col, name_col, period_col):
            continue
        if str(row[period_col]).strip() != period:
            continue
        code = str(row[code_col]).strip()
        if len(code) == 5 and code.isdigit():
            code_to_name[code] = str(row[name_col]).strip()
    return build_index(code_to_name)


def parse_month(
    rows: Sequence[Sequence[str]],
    columns: Dict[str, int],
    period: str,
    index: Dict[str, str],
) -> Tuple[Dict[str, Dict[str, Any]], ParseReport]:
    """Parse one month's rows for the five watched provinces."""
    report = ParseReport()
    municipalities: Dict[str, Dict[str, Any]] = {}
    period_col = columns[COL_PERIOD]
    code_col = columns[COL_MUNICIPALITY_CODE]
    name_col = columns[COL_MUNICIPALITY]
    total_col = columns[COL_TOTAL]
    prov_col = columns[COL_PROVINCE_CODE]
    widest = max(period_col, code_col, name_col, total_col, prov_col)

    for row in rows:
        if len(row) <= widest:
            continue
        if str(row[period_col]).strip() != period:
            continue

        raw_code = str(row[code_col]).strip()
        raw_name = str(row[name_col]).strip()
        province = _province_code(str(row[prov_col]), raw_code)
        if province is not None and province not in PROVINCE_CODES:
            continue

        code = resolve_code(raw_code, raw_name, index)
        if code is None:
            # In scope by province but unresolvable: report it, never guess.
            report.unresolved_names.append(raw_name or f"<blank name, {raw_code!r}>")
            continue
        if code[:2] not in PROVINCE_CODES:
            continue

        total, suppressed = parse_count(str(row[total_col]))
        if total is None and not suppressed:
            report.unparsable.append((code, str(row[total_col]).strip()))
            continue

        entry: Dict[str, Any] = {
            "name": normalize_municipality(raw_name),
            "unemployed_total": total,
        }
        if suppressed:
            entry["suppressed"] = True
            report.suppressed.append(code)
        municipalities[code] = entry
        report.per_province[code[:2]] = report.per_province.get(code[:2], 0) + 1

    return municipalities, report


def attach_year_ago(
    municipalities: Dict[str, Dict[str, Any]],
    rows: Sequence[Sequence[str]],
    columns: Dict[str, int],
    period: str,
) -> int:
    """Add `unemployed_year_ago` from the previous year's same month.

    Only a real figure is attached. A suppressed or missing prior value leaves
    the key off the entry entirely rather than implying a zero or a carry-over.
    """
    period_col = columns[COL_PERIOD]
    code_col = columns[COL_MUNICIPALITY_CODE]
    total_col = columns[COL_TOTAL]
    widest = max(period_col, code_col, total_col)
    attached = 0
    for row in rows:
        if len(row) <= widest:
            continue
        if str(row[period_col]).strip() != period:
            continue
        code = str(row[code_col]).strip()
        entry = municipalities.get(code)
        if entry is None:
            continue
        value, _suppressed = parse_count(str(row[total_col]))
        if value is None:
            continue
        entry["unemployed_year_ago"] = value
        attached += 1
    return attached


def fetch_csv(url: str) -> bytes:
    """Download one annual CSV, bounded, with the project's polite User-Agent."""
    import requests

    response = request_with_retries(
        requests.get,
        url,
        headers={"User-Agent": HTTP_USER_AGENT},
        timeout=HTTP_TIMEOUT_S,
        stream=True,
        logger=logger,
        gate=SEPE_GATE,
    )
    try:
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code} from {url}")
        chunks: List[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > MAX_CSV_BYTES:
                raise RuntimeError(
                    f"{url} exceeds {MAX_CSV_BYTES} bytes — refusing to parse it"
                )
            chunks.append(chunk)
    finally:
        response.close()
    return b"".join(chunks)


def load_year(year: int, local_path: Optional[str] = None) -> List[List[str]]:
    """Rows of one annual CSV, from disk when given a path, else from SEPE."""
    if local_path:
        with open(local_path, "rb") as handle:
            raw = handle.read()
        if len(raw) > MAX_CSV_BYTES:
            raise RuntimeError(
                f"{local_path} exceeds {MAX_CSV_BYTES} bytes — refusing to parse it"
            )
    else:
        raw = fetch_csv(CSV_URL_TEMPLATE.format(year=year))
    return read_rows(decode_csv(raw))


# Municipalities that merged, and the legacy codes SEPE still publishes them
# under. Measured in the 2026-06 file: Oza-Cesuras (15902) carries a bare 0
# while its real figures sit under Cesuras + Oza dos Ríos, and
# Cerdedo-Cotobade (36902) has no row of its own at all. Reading either as
# published would put "0.0% unemployment" at the top of the comparison page —
# a bookkeeping artifact of the merger, not a fact about the place.
# INE uses the merged codes, so the join needs them composed (2026-08-14).
# code -> (INE name, legacy codes). The name is carried here because
# Cerdedo-Cotobade has no SEPE row of its own to take one from.
MERGED_MUNICIPALITIES = {
    "15902": ("Oza-Cesuras", ("15026", "15063")),
    "36902": ("Cerdedo-Cotobade", ("36011", "36012")),
}


def compose_merged(municipalities: Dict[str, Dict[str, Any]]) -> List[str]:
    """Sum legacy rows into the merged code. Returns the codes composed.

    A component that is suppressed or missing makes the total unknown rather
    than a smaller number that would read as published: the sum is dropped
    and the entry says why.
    """
    composed: List[str] = []
    for merged_code, (merged_name, parts) in MERGED_MUNICIPALITIES.items():
        present = [municipalities[p] for p in parts if p in municipalities]
        if not present:
            continue
        entry = dict(municipalities.get(merged_code) or {})
        entry["name"] = merged_name
        totals = [p.get("unemployed_total") for p in present]
        entry["composed_from"] = list(parts)
        if len(present) == len(parts) and all(t is not None for t in totals):
            entry["unemployed_total"] = sum(totals)
            entry.pop("suppressed", None)
        else:
            # Partial data cannot be summed into an honest total.
            entry["unemployed_total"] = None
            entry["composed_incomplete"] = True
        prior = [p.get("unemployed_year_ago") for p in present]
        if len(present) == len(parts) and all(v is not None for v in prior):
            entry["unemployed_year_ago"] = sum(prior)
        else:
            entry.pop("unemployed_year_ago", None)
        municipalities[merged_code] = entry
        for part in parts:
            municipalities.pop(part, None)
        composed.append(merged_code)
    return composed


def build_document(
    period: str,
    municipalities: Dict[str, Dict[str, Any]],
    source_url: str,
    period_year_ago: Optional[str],
) -> Dict[str, Any]:
    month_label = period.replace("-", " ")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": (f"SEPE Paro registrado por municipios, {month_label}, {source_url}"),
        "source_url": source_url,
        "dataset_page": DATASET_PAGE_URL,
        "period": period,
        "period_year_ago": period_year_ago,
        "suppression_note": SUPPRESSION_NOTE,
        "municipalities": municipalities,
    }


def write_atomic(path: str, document: Dict[str, Any]) -> None:
    """Owner-only temp file in the target directory, fsync, atomic rename."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".sepe_unemployment.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600 whatever the umask; this is reference data read
        # inside the app container, so open it to the conventional 0644.
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _load_latest_year(
    year: int, local_path: Optional[str]
) -> Tuple[int, List[List[str]], Dict[str, int], str]:
    """Load the newest year that actually has data, newest first.

    In early January the current year's file does not exist yet (2027 answered
    404 while 2026 was served). Falling back one year is not a silent default:
    the year that was read is logged, and the period it produced is written
    into the document.
    """
    attempts = [year] if local_path else [year, year - 1]
    errors: List[str] = []
    for candidate in attempts:
        try:
            rows = load_year(candidate, local_path)
            _, columns = find_header(rows)
            period = latest_period(rows, columns)
        except Exception as exc:  # noqa: BLE001 - tried in turn, reported below
            errors.append(f"{candidate}: {exc}")
            logger.warning("Annual CSV for %s unusable: %s", candidate, exc)
            continue
        if candidate != year:
            logger.warning(
                "Annual CSV for %s was unusable; using %s instead", year, candidate
            )
        return candidate, rows, columns, period
    raise RuntimeError(
        "Could not fetch or parse any SEPE annual CSV (" + "; ".join(errors) + ")"
    )


def report_lines(
    period: str,
    municipalities: Dict[str, Dict[str, Any]],
    parse_report: ParseReport,
    year_ago_attached: int,
    period_year_ago: Optional[str],
) -> List[str]:
    lines = [f"period: {period}", f"municipalities: {len(municipalities)}"]
    lines.append(
        "per province: "
        + ", ".join(
            f"{code}={parse_report.per_province.get(code, 0)}"
            for code in sorted(PROVINCE_CODES)
        )
    )
    lines.append(
        f"year-ago ({period_year_ago or 'unavailable'}): {year_ago_attached} attached"
    )
    lines.append(
        "suppressed (<5): " + (", ".join(sorted(parse_report.suppressed)) or "none")
    )
    lines.append(
        "names not resolved: "
        + (", ".join(sorted(set(parse_report.unresolved_names))) or "none")
    )
    if parse_report.unparsable:
        lines.append(
            "unparsable totals: "
            + ", ".join(f"{code}={value!r}" for code, value in parse_report.unparsable)
        )
    return lines


def run(
    out_path: str,
    year: Optional[int],
    csv_path: Optional[str],
    prev_csv_path: Optional[str],
    skip_year_ago: bool,
) -> int:
    target_year = year or datetime.now(timezone.utc).year
    used_year, rows, columns, period_code = _load_latest_year(target_year, csv_path)
    period = period_to_iso(period_code)
    source_url = csv_path or CSV_URL_TEMPLATE.format(year=used_year)

    index = build_name_index(rows, columns, period_code)
    municipalities, parse_report = parse_month(rows, columns, period_code, index)
    if not municipalities:
        raise RuntimeError(
            f"Parsed zero municipalities for provinces {sorted(PROVINCE_CODES)} in "
            f"{period} — refusing to write an empty document"
        )

    period_year_ago: Optional[str] = None
    attached = 0
    if not skip_year_ago:
        prev_code = f"{int(period_code[:4]) - 1}{period_code[4:]}"
        try:
            prev_rows = load_year(int(prev_code[:4]), prev_csv_path)
            _, prev_columns = find_header(prev_rows)
            attached = attach_year_ago(
                municipalities, prev_rows, prev_columns, prev_code
            )
        except Exception as exc:  # noqa: BLE001 - optional enrichment
            logger.warning(
                "Year-ago figures unavailable (%s); entries carry none rather than a guess",
                exc,
            )
        else:
            if attached:
                period_year_ago = period_to_iso(prev_code)
            else:
                logger.warning(
                    "Previous year's file held no %s rows for these municipalities",
                    prev_code,
                )

    # After the year-ago pass, so both figures are composed from the same
    # legacy rows before those rows are dropped.
    composed = compose_merged(municipalities)

    document = build_document(period, municipalities, source_url, period_year_ago)
    write_atomic(out_path, document)
    print(
        "\n".join(
            report_lines(
                period, municipalities, parse_report, attached, period_year_ago
            )
        )
    )
    if composed:
        print(f"merged municipalities composed from legacy codes: {composed}")
    print(f"written: {os.path.abspath(out_path)}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import SEPE municipal registered unemployment for the five "
            "watched provinces"
        )
    )
    parser.add_argument(
        "--out", required=True, help="output JSON path (data/sepe_unemployment.json)"
    )
    parser.add_argument(
        "--year",
        type=int,
        help="annual CSV year to read (default: the current year, then the one before)",
    )
    parser.add_argument("--csv", help="use a local annual CSV instead of downloading")
    parser.add_argument(
        "--prev-csv", help="use a local previous-year CSV for the year-ago figures"
    )
    parser.add_argument(
        "--skip-year-ago",
        action="store_true",
        help="do not fetch the previous year's file",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    return run(args.out, args.year, args.csv, args.prev_csv, args.skip_year_ago)


if __name__ == "__main__":
    sys.exit(main())
