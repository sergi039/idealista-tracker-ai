"""Tests for the SEPE municipal unemployment importer. No network.

The fixtures below are small hand-built copies of the two real files this
importer reads, measured on 2026-08-14:

- `_csv_2026()` mimics `Paro_por_municipios_2026_csv.csv`
  (https://sede.sepe.gob.es/es/portaltrabaja/resources/sede/datos_abiertos/
  datos/Paro_por_municipios_2026_csv.csv) — `;`-separated, CRLF, ISO-8859-1,
  a banner row before the header, 20 columns, a header whose names carry stray
  spaces (`Código mes `, ` Municipio`), one row per municipality per month, a
  zero-padded 5-digit INE code in `Codigo Municipio`, INE's inverted-article
  names (`Franco, El`), and SEPE's `<5` confidentiality marker.
- `_csv_2025()` mimics `Paro_por_municipios_2025_csv.csv`, the same layout one
  year earlier — the source of `unemployed_year_ago`.

Real values are used where the test asserts on one: Navia (33041) had 227
registered unemployed in June 2026 and 216 in June 2025; El Franco (33023) had
98 and 124; Pesoz (33048) was suppressed as `<5`.
"""

import os
import stat

import pytest

from utils.import_sepe_unemployment import (
    COL_TOTAL,
    attach_year_ago,
    build_name_index,
    decode_csv,
    find_header,
    latest_period,
    parse_count,
    parse_month,
    period_to_iso,
    read_rows,
    report_lines,
    write_atomic,
)
from utils.municipality_codes import build_index

BANNER = (
    ";;;;PARO REGISTRADO POR MUNICIPIOS DESGLOSADO POR SEXO, TRAMOS DE EDAD Y "
    "SECTOR DE LA ACTIVIDAD ECONÓMICA;;;;;;;;;;;;;;;"
)
# The real header, stray spaces included.
HEADER = (
    "Código mes ;mes;Código de CA;Comunidad Autónoma;Codigo Provincia;Provincia;"
    "Codigo Municipio; Municipio;total Paro Registrado;Paro hombre edad < 25;"
    "Paro hombre edad 25 -45 ;Paro hombre edad >=45;Paro mujer edad < 25;"
    "Paro mujer edad 25 -45 ;Paro mujer edad >=45;Paro Agricultura;"
    "Paro Industria;Paro Construcción;Paro Servicios;Paro Sin empleo Anterior"
)


def _row(period, mes, prov_code, prov_name, muni_code, muni_name, total):
    """One data row padded to the real file's 20 columns."""
    head = [
        period,
        mes,
        "3",
        "Asturias, Principado de",
        prov_code,
        prov_name,
        muni_code,
        muni_name,
        total,
    ]
    return ";".join(head + ["0"] * (20 - len(head)))


def _as_bytes(lines):
    """Join as the real file does: CRLF, ISO-8859-1."""
    return ("\r\n".join(lines) + "\r\n").encode("latin-1")


def _csv_2026():
    return _as_bytes(
        [
            BANNER,
            HEADER,
            # An earlier month, so `latest_period` has something to beat.
            _row("202605", "Mayo de 2026", "33", "Asturias", "33041", "Navia", "231"),
            _row("202606", "Junio de 2026", "33", "Asturias", "33041", "Navia", "227"),
            _row(
                "202606", "Junio de 2026", "33", "Asturias", "33023", "Franco, El", "98"
            ),
            # SEPE's confidentiality marker, not a number.
            _row("202606", "Junio de 2026", "33", "Asturias", "33048", "Pesoz", "<5"),
            _row(
                "202606",
                "Junio de 2026",
                "15",
                "A Coruña",
                "15030",
                "Coruña, A",
                "9560",
            ),
            # Out of scope: province 04 is not one of the five watched.
            _row("202606", "Junio de 2026", "4", "Almería", "04001", "Abla", "65"),
        ]
    )


def _csv_2025():
    return _as_bytes(
        [
            BANNER,
            HEADER,
            _row("202506", "Junio de 2025", "33", "Asturias", "33041", "Navia", "216"),
            _row(
                "202506",
                "Junio de 2025",
                "33",
                "Asturias",
                "33023",
                "Franco, El",
                "124",
            ),
            # Suppressed a year ago too: must not become a number.
            _row("202506", "Junio de 2025", "33", "Asturias", "33048", "Pesoz", "<5"),
        ]
    )


def _parsed(raw=None):
    rows = read_rows(decode_csv(raw or _csv_2026()))
    _, columns = find_header(rows)
    period = latest_period(rows, columns)
    index = build_name_index(rows, columns, period)
    return rows, columns, period, index


# --- decoding and header discovery ---------------------------------------


def test_decode_is_latin1_not_utf8():
    """The response declares UTF-8 and is wrong; latin-1 is what reads it."""
    text = decode_csv(_csv_2026())
    assert "Almería" in text
    assert "ECONÓMICA" in text


def test_find_header_skips_banner_and_strips_stray_spaces():
    rows = read_rows(decode_csv(_csv_2026()))
    idx, columns = find_header(rows)
    assert idx == 1, "the header sits under a banner row, not at row 0"
    # 'Código mes ' and ' Municipio' carry stray spaces in the real file.
    assert columns["Código mes"] == 0
    assert columns["Codigo Municipio"] == 6
    assert columns["Municipio"] == 7
    assert columns[COL_TOTAL] == 8


def test_find_header_raises_when_a_required_column_is_renamed():
    broken = _csv_2026().replace(
        "total Paro Registrado".encode("latin-1"), "Paro total".encode("latin-1")
    )
    with pytest.raises(ValueError, match="No header row"):
        find_header(read_rows(decode_csv(broken)))


# --- value parsing --------------------------------------------------------


@pytest.mark.parametrize(
    "cell,expected",
    [("227", (227, False)), ("0", (0, False)), (" 98 ", (98, False))],
)
def test_parse_count_reads_figures(cell, expected):
    assert parse_count(cell) == expected


@pytest.mark.parametrize("cell", ["<5", "< 5", "<10"])
def test_parse_count_reports_suppression_not_a_number(cell):
    value, suppressed = parse_count(cell)
    assert value is None, "a withheld count must never become a number"
    assert suppressed is True


@pytest.mark.parametrize("cell", ["", "   ", "n/d", "-"])
def test_parse_count_refuses_garbage(cell):
    assert parse_count(cell) == (None, False)


def test_period_to_iso_and_latest_period():
    assert period_to_iso("202606") == "2026-06"
    with pytest.raises(ValueError):
        period_to_iso("2026")
    rows, columns, period, _ = _parsed()
    assert period == "202606", "the newest month wins over 202605"


def test_latest_period_ignores_non_period_rows():
    """A footer row must not become the period the document claims."""
    raw = _as_bytes(
        [
            BANNER,
            HEADER,
            _row("202606", "Junio de 2026", "33", "Asturias", "33041", "Navia", "227"),
            _row("TOTAL", "", "33", "Asturias", "33041", "Navia", "227"),
        ]
    )
    rows = read_rows(decode_csv(raw))
    _, columns = find_header(rows)
    assert latest_period(rows, columns) == "202606"


# --- month parsing --------------------------------------------------------


def test_parse_month_keys_by_ine_code_and_uninverts_names():
    rows, columns, period, index = _parsed()
    municipalities, report = parse_month(rows, columns, period, index)

    assert municipalities["33041"]["unemployed_total"] == 227
    assert municipalities["33041"]["name"] == "Navia"
    # INE's "Franco, El" is displayed as "El Franco".
    assert municipalities["33023"]["unemployed_total"] == 98
    assert municipalities["33023"]["name"] == "El Franco"
    assert municipalities["15030"]["name"] == "A Coruña"
    assert report.unparsable == []


def test_parse_month_skips_provinces_outside_the_five_silently():
    rows, columns, period, index = _parsed()
    municipalities, report = parse_month(rows, columns, period, index)
    assert "04001" not in municipalities
    # Not our province is not a failure to resolve.
    assert report.unresolved_names == []
    assert set(report.per_province) == {"33", "15"}


def test_parse_month_records_suppressed_as_null_never_zero():
    rows, columns, period, index = _parsed()
    municipalities, report = parse_month(rows, columns, period, index)
    pesoz = municipalities["33048"]
    assert pesoz["unemployed_total"] is None
    assert pesoz["suppressed"] is True
    assert report.suppressed == ["33048"]


def test_parse_month_reports_an_unparsable_total_instead_of_storing_it():
    raw = _as_bytes(
        [
            BANNER,
            HEADER,
            _row("202606", "Junio de 2026", "33", "Asturias", "33041", "Navia", "227"),
            _row(
                "202606",
                "Junio de 2026",
                "33",
                "Asturias",
                "33023",
                "Franco, El",
                "n/d",
            ),
        ]
    )
    rows, columns, period, index = _parsed(raw)
    municipalities, report = parse_month(rows, columns, period, index)
    assert "33023" not in municipalities
    assert report.unparsable == [("33023", "n/d")]


# --- code vs name resolution ---------------------------------------------


def test_row_without_a_code_resolves_by_name():
    """The fallback path: a blank code cell resolves through municipality_codes."""
    raw = _as_bytes(
        [
            BANNER,
            HEADER,
            _row("202606", "Junio de 2026", "33", "Asturias", "33041", "Navia", "227"),
            # No code, but a name the index knows (INE spells it "Franco, El").
            _row("202606", "Junio de 2026", "33", "Asturias", "", "Franco, El", "98"),
        ]
    )
    rows = read_rows(decode_csv(raw))
    _, columns = find_header(rows)
    # Index built the way the real run builds it, plus the code-less name.
    index = build_index({"33041": "Navia", "33023": "Franco, El"})
    municipalities, report = parse_month(rows, columns, "202606", index)
    assert municipalities["33023"]["unemployed_total"] == 98
    assert report.unresolved_names == []


def test_unresolvable_name_is_reported_and_skipped_never_guessed():
    raw = _as_bytes(
        [
            BANNER,
            HEADER,
            _row("202606", "Junio de 2026", "33", "Asturias", "33041", "Navia", "227"),
            _row(
                "202606",
                "Junio de 2026",
                "33",
                "Asturias",
                "",
                "Villa Inexistente del Nalón",
                "42",
            ),
        ]
    )
    rows = read_rows(decode_csv(raw))
    _, columns = find_header(rows)
    index = build_index({"33041": "Navia"})
    municipalities, report = parse_month(rows, columns, "202606", index)

    assert list(municipalities) == ["33041"], "the unknown name must not be stored"
    assert report.unresolved_names == ["Villa Inexistente del Nalón"]
    # And its figure appears nowhere under a guessed code.
    assert all(e["unemployed_total"] != 42 for e in municipalities.values())


def test_build_name_index_covers_only_the_watched_provinces():
    _, _, _, index = _parsed()
    assert index["navia"] == "33041"
    # 04001 Abla is outside the five provinces, so it is not indexed.
    assert "abla" not in index


# --- year-ago -------------------------------------------------------------


def test_attach_year_ago_uses_the_same_month_one_year_earlier():
    rows, columns, period, index = _parsed()
    municipalities, _ = parse_month(rows, columns, period, index)

    prev_rows = read_rows(decode_csv(_csv_2025()))
    _, prev_columns = find_header(prev_rows)
    attached = attach_year_ago(municipalities, prev_rows, prev_columns, "202506")

    assert municipalities["33041"]["unemployed_year_ago"] == 216
    assert municipalities["33023"]["unemployed_year_ago"] == 124
    assert attached == 2


def test_year_ago_is_omitted_rather_than_invented():
    rows, columns, period, index = _parsed()
    municipalities, _ = parse_month(rows, columns, period, index)
    prev_rows = read_rows(decode_csv(_csv_2025()))
    _, prev_columns = find_header(prev_rows)
    attach_year_ago(municipalities, prev_rows, prev_columns, "202506")

    # Suppressed a year ago -> no key at all, not 0 and not a carry-over.
    assert "unemployed_year_ago" not in municipalities["33048"]
    # Absent from the previous year's file -> no key either.
    assert "unemployed_year_ago" not in municipalities["15030"]


# --- atomic write ---------------------------------------------------------


def test_write_atomic_mode_content_and_no_leftover_temp(tmp_path):
    out = tmp_path / "sepe_unemployment.json"
    document = {"period": "2026-06", "municipalities": {"33041": {"name": "Navia"}}}
    write_atomic(str(out), document)

    mode = stat.S_IMODE(os.stat(out).st_mode)
    assert mode == 0o644, f"expected 0644 reference data, got {oct(mode)}"

    import json

    assert json.loads(out.read_text(encoding="utf-8"))["period"] == "2026-06"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".sepe")]
    assert leftovers == []


def test_write_atomic_replaces_an_existing_file(tmp_path):
    out = tmp_path / "sepe_unemployment.json"
    write_atomic(str(out), {"period": "2026-05"})
    write_atomic(str(out), {"period": "2026-06"})

    import json

    assert json.loads(out.read_text(encoding="utf-8"))["period"] == "2026-06"
    assert len(list(tmp_path.iterdir())) == 1


def test_report_lines_names_the_gaps():
    rows, columns, period, index = _parsed()
    municipalities, report = parse_month(rows, columns, period, index)
    text = "\n".join(
        report_lines(period_to_iso(period), municipalities, report, 2, "2025-06")
    )
    assert "period: 2026-06" in text
    assert "33048" in text, "a suppressed municipality is named in the report"
    assert "names not resolved: none" in text


class TestMergedMunicipalities:
    """SEPE keeps publishing merged municipalities under their legacy codes.

    Measured in the 2026-06 file: Oza-Cesuras (15902) carries a bare 0 while
    Cesuras + Oza dos Ríos hold the real figures, and Cerdedo-Cotobade
    (36902) has no row at all. INE uses the merged codes, so an uncomposed
    join put "0.0% unemployment" at the top of the comparison page — a
    bookkeeping artifact, not a fact about the place (2026-08-14).
    """

    def test_legacy_rows_are_summed_into_the_merged_code(self):
        from utils.import_sepe_unemployment import compose_merged

        municipalities = {
            "15902": {"name": "Oza-Cesuras", "unemployed_total": 0},
            "15026": {"name": "Cesuras", "unemployed_total": 50},
            "15063": {"name": "Oza dos Ríos", "unemployed_total": 85},
        }
        composed = compose_merged(municipalities)

        assert composed == ["15902"]
        assert municipalities["15902"]["unemployed_total"] == 135
        assert municipalities["15902"]["composed_from"] == ["15026", "15063"]
        assert "15026" not in municipalities, "legacy rows must not double-count"
        assert "15063" not in municipalities

    def test_a_merged_code_absent_from_the_file_is_created(self):
        from utils.import_sepe_unemployment import compose_merged

        municipalities = {
            "36011": {"name": "Cerdedo", "unemployed_total": 39},
            "36012": {"name": "Cotobade", "unemployed_total": 141},
        }
        compose_merged(municipalities)

        assert municipalities["36902"]["unemployed_total"] == 180
        assert municipalities["36902"]["name"] == "Cerdedo-Cotobade"

    def test_a_suppressed_component_makes_the_total_unknown_not_smaller(self):
        from utils.import_sepe_unemployment import compose_merged

        municipalities = {
            "15026": {"name": "Cesuras", "unemployed_total": None, "suppressed": True},
            "15063": {"name": "Oza dos Ríos", "unemployed_total": 85},
        }
        compose_merged(municipalities)

        entry = municipalities["15902"]
        assert entry["unemployed_total"] is None, (
            "a partial sum would read as a published figure"
        )
        assert entry["composed_incomplete"] is True

    def test_year_ago_is_composed_from_the_same_rows(self):
        from utils.import_sepe_unemployment import compose_merged

        municipalities = {
            "15026": {"unemployed_total": 50, "unemployed_year_ago": 55},
            "15063": {"unemployed_total": 85, "unemployed_year_ago": 96},
        }
        compose_merged(municipalities)

        assert municipalities["15902"]["unemployed_year_ago"] == 151

    def test_a_missing_year_ago_component_drops_the_key(self):
        from utils.import_sepe_unemployment import compose_merged

        municipalities = {
            "15026": {"unemployed_total": 50},
            "15063": {"unemployed_total": 85, "unemployed_year_ago": 96},
        }
        compose_merged(municipalities)

        assert "unemployed_year_ago" not in municipalities["15902"]
