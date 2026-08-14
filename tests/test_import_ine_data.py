"""Parsers and failure policy of the INE municipal-data importer.

`utils/import_ine_data.py` reads three live INE sources. No test here touches
the network: every fixture is a small sample hand-trimmed from the *real*
responses downloaded on 2026-08-14 —

- the xlsx fixture mirrors `diccionario26.xlsx` (title row, then a
  CODAUTO/CPRO/CMUN/DC/NOMBRE header, every cell a shared string, codes
  already zero-padded);
- the renta fixtures are verbatim series from
  `DATOS_TABLA/30860?tip=AM&nult=1` (Asturias ADRH): Navia 15629 € and
  "Franco, El" 14713 € for 2023, plus the distrito/sección/other-indicator
  series the parser must skip;
- the padron fixtures are verbatim series from
  `DATOS_TABLA/2886?tip=AM&nult=6`: Navia Total 2020-2025 (8322 -> 8031,
  a -3.5% five-year change) plus the Hombres and province-level series the
  parser must skip;
- the discovery fixtures mirror `OPERACIONES_DISPONIBLES` (operation 22,
  "DPOP") and `TABLAS_OPERACION/22` ("Coruña, A: Población por municipios y
  sexo." and siblings, Codigo PROV-MUN, anchor 2886).

What is pinned beyond parsing: refusals stay honest (a failed province is
named in the source block and its values stay null, never invented), and a
run in which renta failed for *every* province exits non-zero without
writing a file.
"""

import io
import json
import zipfile

import pytest
import requests

from utils import import_ine_data as imp

# --- xlsx fixture -----------------------------------------------------------

_SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _build_xlsx(rows):
    """Build a minimal xlsx like INE's: shared-string cells by default.

    `rows` is a list of rows; each cell is a plain string (stored as a shared
    string, which is how every cell of the real diccionario26.xlsx is stored)
    or a ("n", "123") tuple for a numeric cell.
    """
    shared = []

    def cell(row_index, column_index, value):
        ref = f"{chr(ord('A') + column_index)}{row_index}"
        if isinstance(value, tuple):
            kind, text = value
            assert kind == "n"
            return f'<c r="{ref}"><v>{text}</v></c>'
        if value in shared:
            index = shared.index(value)
        else:
            shared.append(value)
            index = len(shared) - 1
        return f'<c r="{ref}" t="s"><v>{index}</v></c>'

    body = []
    for i, row in enumerate(rows, start=1):
        cells = "".join(cell(i, j, value) for j, value in enumerate(row))
        body.append(f'<row r="{i}">{cells}</row>')
    sheet = f'<worksheet xmlns="{_SHEET_NS}"><sheetData>{"".join(body)}</sheetData></worksheet>'
    sst = (
        f'<sst xmlns="{_SHEET_NS}">'
        + "".join(f"<si><t>{s}</t></si>" for s in shared)
        + "</sst>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", sst)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


DICCIONARIO_ROWS = [
    # Row 1: title; row 2: header — exactly the real file's layout.
    ["Relación de municipios y códigos por comunidades autónomas y provincias"],
    ["CODAUTO", "CPRO", "CMUN", "DC", "NOMBRE"],
    ["03", "33", "041", "1", "Navia"],
    ["03", "33", "023", "6", "Franco, El"],
    ["12", "15", "030", "3", "Coruña, A"],
    ["12", "27", "065", "5", "Vilalba"],
    ["12", "32", "054", "9", "Ourense"],
    ["12", "36", "038", "9", "Pontevedra"],
    # Out-of-scope province (Barcelona): must be dropped.
    ["09", "08", "019", "3", "Barcelona"],
    # Numeric cells with unpadded codes: not the observed INE format (all its
    # cells are strings), but the defensive padding path is worth pinning.
    [("n", "12"), ("n", "27"), ("n", "9"), ("n", "0"), "Barreiros"],
]


def _diccionario_xlsx():
    return _build_xlsx(DICCIONARIO_ROWS)


# --- wstempus fixtures ------------------------------------------------------


def _renta_series(code, name, indicator, value):
    meta = [
        {"Id": 1, "T3_Variable": "Municipios", "Nombre": name, "Codigo": code},
        {"Id": 72, "T3_Variable": "Tipo de dato", "Nombre": "Dato base", "Codigo": ""},
        {"Id": 2, "T3_Variable": "SALDOS CONTABLES", "Nombre": indicator, "Codigo": ""},
    ]
    data = []
    if value is not None:
        data = [
            {
                "Fecha": "2023-01-01T00:00:00.000+01:00",
                "T3_TipoDato": "Definitivo",
                "T3_Periodo": "A",
                "Anyo": 2023,
                "Valor": value,
            }
        ]
    return {
        "COD": f"ADRH-{code}-{indicator[:12]}",
        "Nombre": f"{name}. Dato base. {indicator}. ",
        "T3_Unidad": "Euros",
        "MetaData": meta,
        "Data": data,
    }


# Verbatim structure and values from DATOS_TABLA/30860?tip=AM&nult=1
# (Asturias ADRH, fetched 2026-08-14): Navia and El Franco, year 2023.
RENTA_ASTURIAS = [
    _renta_series("33041", "Navia", "Renta neta media por persona", 15629.0),
    _renta_series("33023", "Franco, El", "Renta neta media por persona", 14713.0),
    # Wrong indicator: skipped.
    _renta_series("33041", "Navia", "Renta neta media por hogar", 38129.0),
    # Distrito series (real shape: `Distritos` variable, 7-digit code) and
    # sección series (10-digit): both skipped.
    {
        "COD": "ADRH84683",
        "Nombre": "Allande distrito 01. Dato base. Renta neta media por persona. ",
        "T3_Unidad": "Euros",
        "MetaData": [
            {
                "Id": 3,
                "T3_Variable": "Distritos",
                "Nombre": "Allande distrito 01",
                "Codigo": "3300101",
            },
            {
                "Id": 72,
                "T3_Variable": "Tipo de dato",
                "Nombre": "Dato base",
                "Codigo": "",
            },
            {
                "Id": 2,
                "T3_Variable": "SALDOS CONTABLES",
                "Nombre": "Renta neta media por persona",
                "Codigo": "",
            },
        ],
        "Data": [{"Anyo": 2023, "Valor": 14000.0}],
    },
    {
        "COD": "ADRH24691",
        "Nombre": "Allande sección 01001. Dato base. Renta neta media por persona. ",
        "T3_Unidad": "Euros",
        "MetaData": [
            {
                "Id": 4,
                "T3_Variable": "Secciones",
                "Nombre": "Allande sección 01001",
                "Codigo": "3300101001",
            },
            {
                "Id": 72,
                "T3_Variable": "Tipo de dato",
                "Nombre": "Dato base",
                "Codigo": "",
            },
            {
                "Id": 2,
                "T3_Variable": "SALDOS CONTABLES",
                "Nombre": "Renta neta media por persona",
                "Codigo": "",
            },
        ],
        "Data": [{"Anyo": 2023, "Valor": 13500.0}],
    },
    # Suppressed municipality (ADRH withholds small populations): the code is
    # recorded with an explicit null, never invented. Synthetic code, real
    # empty-Data shape.
    _renta_series("33099", "Suprimido", "Renta neta media por persona", None),
]


def _padron_series(code, name, sexo, points):
    return {
        "COD": f"DPOP-{code}-{sexo}",
        "Nombre": f"{name}. {sexo}. Total habitantes. Personas. ",
        "T3_Unidad": "Personas",
        "MetaData": [
            {"Id": 5, "T3_Variable": "Municipios", "Nombre": name, "Codigo": code},
            {"Id": 451, "T3_Variable": "Sexo", "Nombre": sexo, "Codigo": "0"},
            {
                "Id": 8677,
                "T3_Variable": "Tamaño de los municipios",
                "Nombre": "Total habitantes",
                "Codigo": "0",
            },
            {
                "Id": 20258,
                "T3_Variable": "Tipo de dato",
                "Nombre": "Personas",
                "Codigo": "",
            },
        ],
        "Data": [
            {"Anyo": year, "Valor": value, "T3_Periodo": "A"} for year, value in points
        ],
    }


# Verbatim values from DATOS_TABLA/2886?tip=AM&nult=6 (fetched 2026-08-14).
NAVIA_POINTS = [
    (2025, 8031.0),
    (2024, 8112.0),
    (2023, 8136.0),
    (2022, 8263.0),
    (2021, 8302.0),
    (2020, 8322.0),
]

PADRON_ASTURIAS = [
    _padron_series("33041", "Navia", "Total", NAVIA_POINTS),
    # Hombres series: skipped, only Total counts.
    _padron_series("33041", "Navia", "Hombres", [(2025, 3885.0), (2020, 4026.0)]),
    # Province-level series (real shape: `Provincias` variable, no
    # `Municipios` entry): skipped.
    {
        "COD": "DPOP15001",
        "Nombre": "Asturias. Total. Total habitantes. Personas. ",
        "T3_Unidad": "Personas",
        "MetaData": [
            {
                "Id": 33,
                "T3_Variable": "Provincias",
                "Nombre": "Asturias",
                "Codigo": "33",
            },
            {"Id": 451, "T3_Variable": "Sexo", "Nombre": "Total", "Codigo": "0"},
        ],
        "Data": [{"Anyo": 2025, "Valor": 1013529.0}],
    },
    # A window too short to hold the base year: population recorded, change
    # honestly null. Synthetic code, real series shape.
    _padron_series("33098", "Corto", "Total", [(2025, 1000.0), (2024, 1010.0)]),
]

# Trimmed from OPERACIONES_DISPONIBLES and TABLAS_OPERACION/22
# (fetched 2026-08-14). Operation 230 (PERE) also mentions the Padrón but its
# tables lack the Asturias anchor; operation 22 is the right one.
OPERACIONES = [
    {
        "Id": 230,
        "Cod_IOE": "85001",
        "Nombre": "Estadística del Padrón de la Población Española Residente en el Extranjero",
        "Codigo": "PERE",
    },
    {
        "Id": 22,
        "Cod_IOE": "30245",
        "Nombre": "Cifras Oficiales de Población de los Municipios Españoles: Revisión del Padrón Municipal",
        "Codigo": "DPOP",
    },
    {
        "Id": 25,
        "Cod_IOE": "30138",
        "Nombre": "Índice de Precios de Consumo",
        "Codigo": "IPC",
    },
]

TABLAS_OP_22 = [
    {"Id": 2852, "Nombre": "Población por provincias y sexo.", "Codigo": "NAC-PROV"},
    {
        "Id": 2886,
        "Nombre": "Asturias: Población por municipios y sexo. ",
        "Codigo": "PROV-MUN",
    },
    {
        "Id": 2868,
        "Nombre": "Coruña, A: Población por municipios y sexo. ",
        "Codigo": "PROV-MUN",
    },
    {
        "Id": 2880,
        "Nombre": "Lugo: Población por municipios y sexo. ",
        "Codigo": "PROV-MUN",
    },
    {
        "Id": 2885,
        "Nombre": "Ourense: Población por municipios y sexo. ",
        "Codigo": "PROV-MUN",
    },
    {
        "Id": 2890,
        "Nombre": "Pontevedra: Población por municipios y sexo. ",
        "Codigo": "PROV-MUN",
    },
]

TABLAS_OP_230 = [
    {"Id": 999, "Nombre": "PERE por país de residencia", "Codigo": "PERE-PAIS"}
]


class FakeClient:
    """Stands in for IneClient: canned bodies per URL, no network."""

    def __init__(self, byte_bodies=None, json_bodies=None, failing=()):
        self.byte_bodies = byte_bodies or {}
        self.json_bodies = json_bodies or {}
        self.failing = set(failing)

    def get_bytes(self, url):
        if url in self.failing:
            raise requests.ConnectionError(f"refused: {url}")
        return self.byte_bodies[url]

    def get_json(self, url):
        if url in self.failing:
            raise requests.ConnectionError(f"refused: {url}")
        return self.json_bodies[url]


# --- parser tests -----------------------------------------------------------


class TestParseDiccionario:
    def test_reads_target_provinces_and_pads_codes(self):
        codes = imp.parse_diccionario(_diccionario_xlsx())
        assert codes["33041"] == "Navia"
        assert codes["33023"] == "Franco, El"
        assert codes["15030"] == "Coruña, A"
        # Numeric, unpadded cells still produce a 5-digit code.
        assert codes["27009"] == "Barreiros"
        # Out-of-scope province dropped.
        assert not any(code.startswith("08") for code in codes)
        assert len(codes) == 7

    def test_missing_header_is_a_loud_failure(self):
        broken = _build_xlsx([["no", "header", "here"]])
        with pytest.raises(ValueError):
            imp.parse_diccionario(broken)

    def test_dtd_in_xml_is_refused(self):
        # Entity expansion on an untrusted download: refused, not parsed.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(
                "xl/worksheets/sheet1.xml",
                f'<!DOCTYPE x [<!ENTITY a "b">]><worksheet xmlns="{_SHEET_NS}"/>',
            )
        with pytest.raises(ValueError):
            imp.parse_diccionario(buffer.getvalue())


class TestParseRentaTable:
    def test_takes_municipality_rows_for_the_person_indicator(self):
        parsed = imp.parse_renta_table(RENTA_ASTURIAS)
        assert parsed["33041"] == {"name": "Navia", "renta": 15629, "year": 2023}
        assert parsed["33023"] == {"name": "Franco, El", "renta": 14713, "year": 2023}

    def test_distritos_and_secciones_are_skipped(self):
        parsed = imp.parse_renta_table(RENTA_ASTURIAS)
        assert "3300101" not in parsed
        assert "3300101001" not in parsed
        assert not any(len(code) != 5 for code in parsed)

    def test_suppressed_data_stays_an_explicit_null(self):
        parsed = imp.parse_renta_table(RENTA_ASTURIAS)
        assert parsed["33099"] == {"name": "Suprimido", "renta": None, "year": None}

    def test_other_indicators_do_not_overwrite(self):
        # "por hogar" (38129) must never leak into the per-person value.
        parsed = imp.parse_renta_table(RENTA_ASTURIAS)
        assert parsed["33041"]["renta"] == 15629


class TestParsePadronTable:
    def test_population_and_five_year_change(self):
        parsed = imp.parse_padron_table(PADRON_ASTURIAS)
        navia = parsed["33041"]
        assert navia["population"] == 8031
        assert navia["population_year"] == 2025
        # (8031 - 8322) / 8322 = -3.497% -> -3.5
        assert navia["population_5y_change_pct"] == -3.5

    def test_only_the_total_sexo_series_counts(self):
        parsed = imp.parse_padron_table(PADRON_ASTURIAS)
        # The Hombres series (3885) must not shadow the Total (8031).
        assert parsed["33041"]["population"] == 8031

    def test_province_level_series_is_skipped(self):
        parsed = imp.parse_padron_table(PADRON_ASTURIAS)
        assert "33" not in parsed

    def test_missing_base_year_means_null_change_not_zero(self):
        parsed = imp.parse_padron_table(PADRON_ASTURIAS)
        short = parsed["33098"]
        assert short["population"] == 1000
        assert short["population_5y_change_pct"] is None


class TestDiscoverPadronTables:
    def _client(self, tables_22=TABLAS_OP_22):
        return FakeClient(
            json_bodies={
                imp.OPERACIONES_URL: OPERACIONES,
                imp.TABLAS_OPERACION_URL.format(operation=230): TABLAS_OP_230,
                imp.TABLAS_OPERACION_URL.format(operation=22): tables_22,
            }
        )

    def test_finds_the_operation_by_its_asturias_anchor(self):
        found, notes = imp.discover_padron_tables(self._client())
        assert found == {
            "33": "2886",
            "15": "2868",
            "27": "2880",
            "32": "2885",
            "36": "2890",
        }
        assert notes == []

    def test_missing_province_is_skipped_and_named(self):
        without_ourense = [t for t in TABLAS_OP_22 if t["Id"] != 2885]
        found, notes = imp.discover_padron_tables(self._client(without_ourense))
        assert "32" not in found
        assert any("province 32" in note for note in notes)

    def test_ambiguous_match_is_skipped_not_guessed(self):
        ambiguous = TABLAS_OP_22 + [
            {
                "Id": 9999,
                "Nombre": "Lugo: Población por municipios y sexo (serie antigua).",
                "Codigo": "PROV-MUN",
            }
        ]
        found, notes = imp.discover_padron_tables(self._client(ambiguous))
        assert "27" not in found
        assert any("province 27" in note for note in notes)


# --- end-to-end run ---------------------------------------------------------


def _renta_url(table):
    return imp.DATOS_TABLA_URL.format(table=table, nult=1)


def _padron_url(table):
    return imp.DATOS_TABLA_URL.format(table=table, nult=6)


def _full_client(failing=()):
    json_bodies = {
        imp.OPERACIONES_URL: OPERACIONES,
        imp.TABLAS_OPERACION_URL.format(operation=230): TABLAS_OP_230,
        imp.TABLAS_OPERACION_URL.format(operation=22): TABLAS_OP_22,
        # Renta: Asturias verbatim; the Galician tables carry one
        # municipality each (same real series shape, synthetic values).
        _renta_url("30860"): RENTA_ASTURIAS,
        _renta_url("30989"): [
            _renta_series("15030", "Coruña, A", "Renta neta media por persona", 16000.0)
        ],
        _renta_url("31133"): [
            _renta_series("32054", "Ourense", "Renta neta media por persona", 11000.0)
        ],
        _renta_url("31160"): [
            _renta_series(
                "36038", "Pontevedra", "Renta neta media por persona", 14000.0
            )
        ],
        # Lugo's renta table (31088) is deliberately absent from this dict —
        # tests that fail it list it in `failing`.
        # Padron: only Asturias and A Coruña answer; Lugo/Ourense/Pontevedra
        # fetches fail, so their population must stay null.
        _padron_url("2886"): PADRON_ASTURIAS,
        _padron_url("2868"): [
            _padron_series(
                "15030", "Coruña, A", "Total", [(2025, 249570.0), (2020, 247000.0)]
            )
        ],
    }
    return FakeClient(
        byte_bodies={imp.DICCIONARIO_URL: _diccionario_xlsx()},
        json_bodies=json_bodies,
        failing=failing,
    )


class TestRun:
    def test_writes_document_with_honest_gaps(self, tmp_path):
        out = tmp_path / "ine_municipal.json"
        failing = {
            imp.DATOS_TABLA_URL.format(table="31088", nult=1),  # Lugo renta
            imp.DATOS_TABLA_URL.format(table="2880", nult=6),  # Lugo padron
            imp.DATOS_TABLA_URL.format(table="2885", nult=6),  # Ourense padron
            imp.DATOS_TABLA_URL.format(table="2890", nult=6),  # Pontevedra padron
        }
        assert imp.run(str(out), _full_client(failing)) == 0
        document = json.loads(out.read_text())

        municipalities = document["municipalities"]
        # 7 in-scope dictionary rows; Barcelona is out.
        assert len(municipalities) == 7
        assert municipalities["33041"] == {
            "name": "Navia",
            "province": "33",
            "renta_media_persona": 15629,
            "renta_year": 2023,
            "population": 8031,
            "population_5y_change_pct": -3.5,
            "population_year": 2025,
        }
        franco = municipalities["33023"]
        assert franco["renta_media_persona"] == 14713
        assert franco["population"] is None  # not in the padron fixture

        # Lugo's renta failed: null value, named in the source block, and no
        # median fabricated for the province.
        assert municipalities["27065"]["renta_media_persona"] is None
        assert document["source"]["renta_missing_provinces"] == ["27"]
        assert "27" not in document["province_medians"]
        medians = document["province_medians"]
        assert medians["33"]["renta_media_persona"] == pytest.approx(
            (15629 + 14713) / 2
        )
        assert medians["15"]["renta_media_persona"] == 16000
        # Failed padron provinces are named.
        notes = " ".join(document["source"]["population_notes"])
        for province in ("27", "32", "36"):
            assert f"population missing for province {province}" in notes
        assert document["generated_at"]

    def test_all_renta_failing_exits_nonzero_and_writes_nothing(self, tmp_path):
        out = tmp_path / "ine_municipal.json"
        failing = {
            imp.DATOS_TABLA_URL.format(table=table, nult=1)
            for table in imp.RENTA_TABLES.values()
        }
        assert imp.run(str(out), _full_client(failing)) == 1
        assert not out.exists()

    def test_failed_dictionary_is_fatal(self, tmp_path):
        out = tmp_path / "ine_municipal.json"
        assert imp.run(str(out), _full_client({imp.DICCIONARIO_URL})) == 1
        assert not out.exists()
