"""Regenerate reference/legal/asturias_concejos.json from the INE reference.

The snapshot is the ONLY runtime identity source for /construccion (see
services/concejo_legal.py). It is committed and rides inside the image;
data/ine_municipal.json is bind-mounted and cached, which is exactly why it
must not serve identity at runtime. CI compares the two, so regenerating and
committing is the whole update path. Free: no network, reads one local file.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "ine_municipal.json"
TARGET = ROOT / "reference" / "legal" / "asturias_concejos.json"


def build() -> dict:
    municipalities = json.loads(SOURCE.read_text(encoding="utf-8"))["municipalities"]
    concejos = {
        code: row["name"]
        for code, row in sorted(municipalities.items())
        if row.get("province") == "33"
    }
    if len(concejos) != 78:
        raise SystemExit(
            f"Asturias must be exactly 78 concejos, got {len(concejos)} — refusing"
        )
    return {"source": "data/ine_municipal.json", "concejos": concejos}


if __name__ == "__main__":
    TARGET.write_text(
        json.dumps(build(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"wrote {TARGET} (78 concejos)")
