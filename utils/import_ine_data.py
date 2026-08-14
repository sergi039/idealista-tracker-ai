"""Import INE municipal data: codes, ADRH renta, Padrón population trend.

    python -m utils.import_ine_data --out data/ine_municipal.json

Three free INE sources, ~13 polite HTTP requests total, no credentials:

- **Municipality codes** — the official dictionary `diccionario26.xlsx`
  (CODAUTO/CPRO/CMUN/DC/NOMBRE). The 5-digit code is CPRO zero-padded to 2
  plus CMUN zero-padded to 3. Only the five watched provinces are kept
  (see `utils.municipality_codes.PROVINCE_CODES`).
- **Renta neta media por persona** — ADRH per-province jaxiT3 tables,
  ids verified live on 2026-08-13 (see `RENTA_TABLES`). Fetched through the
  wstempus JSON API with `tip=AM`, which carries typed metadata per series:
  municipality rows are the ones whose `MetaData` names the `Municipios`
  variable with a 5-digit code — distritos (7 digits) and secciones (10)
  identify themselves and are skipped, no label parsing involved. The JSON
  API was chosen over the csv_bdsc endpoint because its `Valor` is a plain
  number; the CSV renders 15629 as "15.629" (Spanish locale) and invites a
  thousand-fold parsing error.
- **Population trend** — Padrón "Cifras oficiales de población de los
  municipios" per-province tables. Only the Asturias id (2886) is known and
  pinned; the Galician siblings are *discovered* by finding the operation
  whose table list contains that anchor and matching the sibling tables by
  province label and table code. A province whose table cannot be discovered
  or fetched gets `"population": null` and a note in the source block —
  never an invented number.

Failure policy (the #98 lesson, applied to a free API): every per-province
failure is logged and recorded in the output's `source` block, a partial file
says what it is missing, and a run in which *no* province yielded renta exits
non-zero without writing anything — a file that silently lacks the one field
the caller wants is worse than no file.

The xlsx is read with a deliberately minimal reader (zipfile + ElementTree)
instead of a spreadsheet dependency: the dictionary is one sheet of shared
strings, the project's locked dependencies do not include openpyxl, and this
slice does not touch `pyproject.toml`/`uv.lock`. The reader refuses DTDs and
bounds download and decompressed sizes — the body is an HTTP download and is
treated as untrusted input.
"""

import argparse
import io
import json
import logging
import os
import re
import statistics
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from utils.http import HTTP_USER_AGENT, request_with_retries
from utils.municipality_codes import PROVINCE_CODES

logger = logging.getLogger(__name__)

DICCIONARIO_URL = "https://www.ine.es/daco/daco42/codmun/diccionario26.xlsx"

# ADRH "Indicadores de renta media y mediana" per-province jaxiT3 table ids,
# verified against the live site on 2026-08-13.
RENTA_TABLES = {
    "15": "30989",  # A Coruña
    "27": "31088",  # Lugo
    "32": "31133",  # Ourense
    "33": "30860",  # Asturias
    "36": "31160",  # Pontevedra
}
RENTA_INDICATOR = "Renta neta media por persona"

# Padrón "Cifras oficiales de población de los municipios": the one table id
# known ahead of time. The Galician provinces are discovered from the
# operation this anchor belongs to (see discover_padron_tables).
PADRON_ASTURIAS_TABLE = 2886
PADRON_YEARS_BACK = 5

DATOS_TABLA_URL = (
    "https://servicios.ine.es/wstempus/js/ES/DATOS_TABLA/{table}?tip=AM&nult={nult}"
)
OPERACIONES_URL = "https://servicios.ine.es/wstempus/js/ES/OPERACIONES_DISPONIBLES"
TABLAS_OPERACION_URL = (
    "https://servicios.ine.es/wstempus/js/ES/TABLAS_OPERACION/{operation}"
)

# How INE spells each province in table titles ("Coruña, A: Población por
# municipios y sexo."). Used only to pick the right table out of a list.
PROVINCE_LABELS = {
    "15": "Coruña, A",
    "27": "Lugo",
    "32": "Ourense",
    "33": "Asturias",
    "36": "Pontevedra",
}

REQUEST_TIMEOUT_S = 30
SLEEP_BETWEEN_REQUESTS_S = 1.0
# The largest body in practice is an ADRH province table (~10 MB of JSON,
# secciones included). The caps bound an untrusted download, they are not
# quotas to grow into.
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_XLSX_MEMBER_BYTES = 50 * 1024 * 1024

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# What a per-source fetch/parse failure looks like. Deliberately narrow:
# a programming error still crashes loudly instead of being logged away.
FETCH_ERRORS = (
    requests.RequestException,
    ValueError,  # includes json.JSONDecodeError and our own refusals
    KeyError,
    OSError,
    zipfile.BadZipFile,
    ET.ParseError,
)


class IneClient:
    """Polite HTTP client for INE: descriptive UA, timeout, retries, pacing.

    One short sleep before every request after the first — the run makes about
    thirteen downloads and there is no reason to make them back to back.
    """

    def __init__(self, sleep_s: float = SLEEP_BETWEEN_REQUESTS_S):
        self._session = requests.Session()
        self._session.headers["User-Agent"] = HTTP_USER_AGENT
        self._sleep_s = sleep_s
        self._requests_made = 0

    def get_bytes(self, url: str) -> bytes:
        if self._requests_made and self._sleep_s > 0:
            time.sleep(self._sleep_s)
        self._requests_made += 1
        response = request_with_retries(
            self._session.get,
            url,
            timeout=REQUEST_TIMEOUT_S,
            logger=logger,
            stream=True,
        )
        try:
            response.raise_for_status()
            chunks: List[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=1 << 16):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"{url} exceeds {MAX_DOWNLOAD_BYTES} bytes; refusing to read further"
                    )
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            response.close()

    def get_json(self, url: str) -> Any:
        return json.loads(self.get_bytes(url).decode("utf-8"))


# --- xlsx -------------------------------------------------------------------


def _xml_root(data: bytes) -> ET.Element:
    """Parse XML from a downloaded body, refusing DTDs.

    INE's workbook parts carry no DOCTYPE; accepting one would open entity
    expansion on untrusted input, so any inline DTD is treated as a refusal.
    """
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("XML with a DTD refused (untrusted download)")
    return ET.fromstring(data)


def _zip_member(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_XLSX_MEMBER_BYTES:
        raise ValueError(
            f"xlsx member {name} decompresses to {info.file_size} bytes; refused"
        )
    return zf.read(name)


def _cell_column(ref: str) -> int:
    """0-based column index from a cell reference like 'B12'."""
    column = 0
    for ch in ref:
        if not ch.isalpha():
            break
        column = column * 26 + (ord(ch.upper()) - ord("A") + 1)
    if column == 0:
        raise ValueError(f"Cell reference without a column: {ref!r}")
    return column - 1


def _cell_value(cell: ET.Element, shared: List[str]) -> str:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(_XLSX_NS + "t"))
    v = cell.find(_XLSX_NS + "v")
    if v is None or v.text is None:
        return ""
    if kind == "s":
        return shared[int(v.text)]
    return v.text


def xlsx_rows(data: bytes) -> List[List[str]]:
    """Read the first worksheet of an xlsx as rows of strings.

    A minimal reader for INE's dictionary workbook — one sheet, shared
    strings, no formulas — not a general xlsx library. Cells keep their
    sheet positions; missing cells read as "".
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            sst = _xml_root(_zip_member(zf, "xl/sharedStrings.xml"))
            for si in sst.iter(_XLSX_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_XLSX_NS + "t")))
        sheets = sorted(
            n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
        )
        if not sheets:
            raise ValueError("xlsx has no xl/worksheets/sheetN.xml member")
        root = _xml_root(_zip_member(zf, sheets[0]))
        rows: List[List[str]] = []
        for row in root.iter(_XLSX_NS + "row"):
            values: List[str] = []
            for cell in row.iter(_XLSX_NS + "c"):
                ref = cell.get("r")
                column = _cell_column(ref) if ref else len(values)
                while len(values) <= column:
                    values.append("")
                values[column] = _cell_value(cell, shared)
            rows.append(values)
        return rows


def _pad_code(value: str, width: int) -> Optional[str]:
    """Zero-pad a numeric code cell; None when the cell is not a code."""
    text = value.strip()
    if text.endswith(".0"):  # a numeric cell rendered as float text
        text = text[:-2]
    if not text.isdigit():
        return None
    return text.zfill(width)


def parse_diccionario(data: bytes) -> Dict[str, str]:
    """{5-digit code: INE name} for the watched provinces.

    Mirrors the real diccionario26.xlsx (downloaded 2026-08-14): a title row,
    then a CODAUTO/CPRO/CMUN/DC/NOMBRE header, then one row per municipality
    with every cell a shared string and codes already zero-padded. The header
    is located by name, not position, and the padding is applied regardless.
    """
    rows = xlsx_rows(data)
    header_index = None
    columns: Dict[str, int] = {}
    for i, row in enumerate(rows):
        names = {
            value.strip().upper(): j for j, value in enumerate(row) if value.strip()
        }
        if {"CPRO", "CMUN", "NOMBRE"} <= set(names):
            header_index = i
            columns = names
            break
    if header_index is None:
        raise ValueError("diccionario sheet has no CPRO/CMUN/NOMBRE header row")

    out: Dict[str, str] = {}
    for row in rows[header_index + 1 :]:
        needed = max(columns["CPRO"], columns["CMUN"], columns["NOMBRE"])
        if len(row) <= needed:
            continue
        cpro = _pad_code(row[columns["CPRO"]], 2)
        cmun = _pad_code(row[columns["CMUN"]], 3)
        name = row[columns["NOMBRE"]].strip()
        if cpro is None or cmun is None or not name:
            logger.warning("Skipping malformed diccionario row: %r", row)
            continue
        if cpro not in PROVINCE_CODES:
            continue
        out[cpro + cmun] = name
    return out


# --- wstempus JSON series ---------------------------------------------------


def _series_municipality(series: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The Municipios metadata entry, or None for any other territorial level.

    ADRH tables interleave municipality, distrito and sección series; the
    latter identify themselves through their own variables (and 7/10-digit
    codes), so a series is municipal only when it carries `Municipios` with a
    5-digit code and neither of the finer variables.
    """
    meta = series.get("MetaData") or []
    entry = None
    for m in meta:
        variable = m.get("T3_Variable")
        if variable in ("Distritos", "Secciones"):
            return None
        if variable == "Municipios":
            entry = m
    if entry is None:
        return None
    code = (entry.get("Codigo") or "").strip()
    if not re.fullmatch(r"\d{5}", code):
        return None
    return entry


def parse_renta_table(series_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Municipality renta from a DATOS_TABLA?tip=AM&nult=1 payload.

    Returns {code: {"name", "renta", "year"}}. A municipality whose series
    carries no data point keeps an explicit None — ADRH suppresses small
    populations, and inventing a number is not this module's job.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for series in series_list:
        municipality = _series_municipality(series)
        if municipality is None:
            continue
        meta = series.get("MetaData") or []
        if not any((m.get("Nombre") or "").strip() == RENTA_INDICATOR for m in meta):
            continue
        code = municipality["Codigo"].strip()
        if code in out:
            logger.warning("Duplicate renta series for %s; keeping the first", code)
            continue
        record: Dict[str, Any] = {
            "name": (municipality.get("Nombre") or "").strip(),
            "renta": None,
            "year": None,
        }
        points = series.get("Data") or []
        if points:
            value = points[0].get("Valor")
            if value is not None:
                as_float = float(value)
                record["renta"] = int(as_float) if as_float.is_integer() else as_float
                record["year"] = points[0].get("Anyo")
        out[code] = record
    return out


def parse_padron_table(
    series_list: List[Dict[str, Any]], years_back: int = PADRON_YEARS_BACK
) -> Dict[str, Dict[str, Any]]:
    """Municipality population and N-year change from a Padrón table payload.

    Expects `tip=AM&nult=<years_back+1>`. Only the `Sexo == Total` series
    counts. The change is None when the base year is absent from the window —
    an unknown trend is reported as unknown, not as zero.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for series in series_list:
        municipality = _series_municipality(series)
        if municipality is None:
            continue
        meta = series.get("MetaData") or []
        sexo = next((m for m in meta if m.get("T3_Variable") == "Sexo"), None)
        if sexo is not None and (sexo.get("Nombre") or "").strip() != "Total":
            continue
        code = municipality["Codigo"].strip()
        if code in out:
            logger.warning("Duplicate padron series for %s; keeping the first", code)
            continue
        points = {
            point["Anyo"]: point["Valor"]
            for point in series.get("Data") or []
            if point.get("Anyo") is not None and point.get("Valor") is not None
        }
        record: Dict[str, Any] = {
            "population": None,
            "population_year": None,
            "population_5y_change_pct": None,
        }
        if points:
            latest_year = max(points)
            latest = points[latest_year]
            record["population"] = int(latest)
            record["population_year"] = latest_year
            base = points.get(latest_year - years_back)
            if base:
                record["population_5y_change_pct"] = round(
                    (latest - base) / base * 100, 1
                )
        out[code] = record
    return out


def _label_norm(text: str) -> str:
    """Casefold + accent-strip for comparing INE titles. Articles stay put —
    this compares full table titles, not municipality names."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", bare).strip()


def discover_padron_tables(client: "IneClient") -> Tuple[Dict[str, str], List[str]]:
    """Find the per-province Padrón table ids around the Asturias anchor.

    Scans the wstempus operations for ones mentioning the Padrón, and accepts
    the operation whose table list contains `PADRON_ASTURIAS_TABLE` — the
    anchor proves the operation is the right one. Siblings must share the
    anchor's table `Codigo` and open with the province label ("Coruña, A: …").
    Returns ({province: table_id}, [notes]); a province that cannot be
    matched unambiguously is left out and named in the notes.
    """
    notes: List[str] = []
    operations = client.get_json(OPERACIONES_URL)
    candidates = [
        op for op in operations if "padron" in _label_norm(str(op.get("Nombre", "")))
    ]
    if not candidates:
        return {}, ["no wstempus operation mentions the Padrón"]

    for operation in candidates:
        tables = client.get_json(TABLAS_OPERACION_URL.format(operation=operation["Id"]))
        anchor = next((t for t in tables if t.get("Id") == PADRON_ASTURIAS_TABLE), None)
        if anchor is None:
            continue
        found = {"33": str(PADRON_ASTURIAS_TABLE)}
        anchor_codigo = anchor.get("Codigo")
        for province, label in PROVINCE_LABELS.items():
            if province == "33":
                continue
            prefix = _label_norm(label) + ":"
            matches = [
                t
                for t in tables
                if t.get("Codigo") == anchor_codigo
                and _label_norm(str(t.get("Nombre", ""))).startswith(prefix)
            ]
            if len(matches) == 1:
                found[province] = str(matches[0]["Id"])
            else:
                notes.append(
                    f"province {province}: {len(matches)} candidate tables in "
                    f"operation {operation['Id']}, population skipped"
                )
        return found, notes

    return {}, [
        f"no Padrón operation's tables contain the anchor {PADRON_ASTURIAS_TABLE}"
    ]


# --- composition ------------------------------------------------------------


def compose_output(
    codes: Dict[str, str],
    renta: Dict[str, Dict[str, Any]],
    padron: Dict[str, Dict[str, Any]],
    source: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the output document. Missing values stay explicit nulls."""
    municipalities: Dict[str, Dict[str, Any]] = {}
    for code in sorted(codes):
        renta_row = renta.get(code) or {}
        padron_row = padron.get(code) or {}
        municipalities[code] = {
            "name": codes[code],
            "province": code[:2],
            "renta_media_persona": renta_row.get("renta"),
            "renta_year": renta_row.get("year"),
            "population": padron_row.get("population"),
            "population_5y_change_pct": padron_row.get("population_5y_change_pct"),
            "population_year": padron_row.get("population_year"),
        }

    medians: Dict[str, Dict[str, Any]] = {}
    for province in sorted({code[:2] for code in municipalities}):
        values = [
            row["renta_media_persona"]
            for code, row in municipalities.items()
            if code[:2] == province and row["renta_media_persona"] is not None
        ]
        if values:
            medians[province] = {"renta_media_persona": statistics.median(values)}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "municipalities": municipalities,
        "province_medians": medians,
    }


def write_atomic(path: str, document: Dict[str, Any]) -> None:
    """Write the JSON to a temp file in the target directory, fsync, rename."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".ine_municipal.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=1, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def run(out_path: str, client: IneClient) -> int:
    """Fetch everything, compose, write. Returns the process exit code."""
    # 1. Codes: without the dictionary there is nothing to key on — fatal.
    try:
        codes = parse_diccionario(client.get_bytes(DICCIONARIO_URL))
    except FETCH_ERRORS as exc:
        logger.error("Municipality dictionary failed: %s", exc)
        return 1
    per_province = {
        p: sum(1 for c in codes if c.startswith(p)) for p in sorted(PROVINCE_CODES)
    }
    empty = [p for p, n in per_province.items() if n == 0]
    if empty:
        logger.error(
            "Dictionary has no municipalities for provinces %s; refusing", empty
        )
        return 1
    logger.info("Dictionary: %d municipalities %s", len(codes), per_province)

    # 2. Renta per province.
    renta: Dict[str, Dict[str, Any]] = {}
    renta_years: set = set()
    renta_failed: List[str] = []
    for province, table in sorted(RENTA_TABLES.items()):
        url = DATOS_TABLA_URL.format(table=table, nult=1)
        try:
            parsed = parse_renta_table(client.get_json(url))
            if not parsed:
                raise ValueError(f"table {table} parsed to zero municipality rows")
        except FETCH_ERRORS as exc:
            logger.error(
                "Renta for province %s (table %s) failed: %s", province, table, exc
            )
            renta_failed.append(province)
            continue
        unknown = [code for code in parsed if code not in codes]
        if unknown:
            logger.warning(
                "Renta table %s carries %d codes absent from the dictionary "
                "(kept out of the output): %s",
                table,
                len(unknown),
                unknown[:5],
            )
        renta.update({code: row for code, row in parsed.items() if code in codes})
        renta_years.update(
            row["year"] for row in parsed.values() if row["year"] is not None
        )
        logger.info("Renta province %s: %d municipalities", province, len(parsed))
    if len(renta_failed) == len(RENTA_TABLES):
        logger.error("Renta failed for every province; not writing %s", out_path)
        return 1

    # 3. Population: discovery, then per-province fetch. Never fatal.
    padron: Dict[str, Dict[str, Any]] = {}
    padron_years: set = set()
    padron_notes: List[str] = []
    try:
        padron_tables, padron_notes = discover_padron_tables(client)
    except FETCH_ERRORS as exc:
        logger.error("Padron table discovery failed: %s", exc)
        padron_tables = {}
        padron_notes = [f"discovery failed: {exc}"]
    padron_failed = [p for p in sorted(PROVINCE_CODES) if p not in padron_tables]
    for province, table in sorted(padron_tables.items()):
        url = DATOS_TABLA_URL.format(table=table, nult=PADRON_YEARS_BACK + 1)
        try:
            parsed = parse_padron_table(client.get_json(url))
            if not parsed:
                raise ValueError(f"table {table} parsed to zero municipality rows")
        except FETCH_ERRORS as exc:
            logger.error(
                "Padron for province %s (table %s) failed: %s", province, table, exc
            )
            padron_failed.append(province)
            continue
        padron.update({code: row for code, row in parsed.items() if code in codes})
        padron_years.update(
            row["population_year"] for row in parsed.values() if row["population_year"]
        )
        logger.info("Padron province %s: %d municipalities", province, len(parsed))
    for province in padron_failed:
        padron_notes.append(f"population missing for province {province}")

    # 4. Source block: what was read, and what is missing.
    renta_tables_note = "/".join(RENTA_TABLES[p] for p in sorted(RENTA_TABLES))
    renta_year_note = ",".join(str(y) for y in sorted(renta_years)) or "none"
    source: Dict[str, Any] = {
        "renta": (
            f"INE ADRH jaxiT3 tables {renta_tables_note} (wstempus JSON API), "
            f"year {renta_year_note}"
        ),
        "population": (
            "INE Padrón 'Cifras oficiales de población de los municipios' "
            f"tables {sorted(padron_tables.items())}, years {sorted(padron_years) or 'none'}"
        ),
        "codes": f"INE diccionario26.xlsx ({DICCIONARIO_URL})",
    }
    if renta_failed:
        source["renta_missing_provinces"] = sorted(renta_failed)
    if padron_notes:
        source["population_notes"] = padron_notes

    document = compose_output(codes, renta, padron, source)
    write_atomic(out_path, document)
    with_renta = sum(
        1
        for row in document["municipalities"].values()
        if row["renta_media_persona"] is not None
    )
    with_population = sum(
        1
        for row in document["municipalities"].values()
        if row["population"] is not None
    )
    logger.info(
        "Wrote %s: %d municipalities (%d with renta, %d with population), medians for %s",
        out_path,
        len(document["municipalities"]),
        with_renta,
        with_population,
        sorted(document["province_medians"]),
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import INE municipal codes, ADRH renta and Padrón population "
        "into a single JSON data file."
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output path, e.g. data/ine_municipal.json (written atomically)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    return run(args.out, IneClient())


if __name__ == "__main__":
    sys.exit(main())
