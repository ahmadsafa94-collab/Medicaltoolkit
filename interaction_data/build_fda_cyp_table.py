"""
Rebuilds fda_cyp_table.json from the FDA's "For Healthcare Professionals |
FDA's Examples of Drugs that Interact with CYP Enzymes and Transporter
Systems" page (US government work, public domain):
https://www.fda.gov/drugs/drug-interactions-labeling/healthcare-professionals-fdas-examples-drugs-interact-cyp-enzymes-and-transporter-systems

Run by hand when the FDA updates the page (needs pandas + lxml, which the
bot itself does not):
    python interaction_data/build_fda_cyp_table.py [saved_page.html]
"""

import html as html_lib
import json
import os
import re
import sys
import urllib.request

import pandas as pd

URL = ("https://www.fda.gov/drugs/drug-interactions-labeling/"
       "healthcare-professionals-fdas-examples-drugs-interact-cyp-enzymes-and-transporter-systems")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fda_cyp_table.json")

ENZYME_RE = re.compile(r"2C19|2C9|2C8|2B6|2D6|1A2|3A")
TRANSPORTER_RE = re.compile(r"P-gp|BCRP|OATP1B1|OATP1B3|OATP1B|OAT1|OAT3|OCT2|MATE1|MATE2-K")
COLUMNS = {
    "CYP Strg INH": ("inhibits", "strong"),
    "CYP Mod INH": ("inhibits", "moderate"),
    "CYP WK INH": ("inhibits", "weak"),
    "CYP Strg IND": ("induces", "strong"),
    "CYP Mod IND": ("induces", "moderate"),
    "CYP WK IND": ("induces", "weak"),
    "CYP SENS SUB": ("substrate_of", "sensitive"),
    "CYP Mod SENS SUB": ("substrate_of", "moderate sensitive"),
    "TRNSP INH": ("transporter_inhibits", None),
    "TRNSP SUB": ("transporter_substrate", None),
}


def _name_and_notes(raw: str) -> tuple[str, list[str]]:
    raw = raw.replace("\xa0", " ").strip()
    m = re.match(r"^(.*?[A-Za-z)’'])\s*([\d,\s]+)$", raw)
    if not m:
        return raw.lower(), []
    return m.group(1).strip().lower(), re.findall(r"\d+", m.group(2))


def _cell(text: str, pattern) -> tuple[list[str], list[str]]:
    text = text.replace("\xa0", " ")
    tokens = [m.group(0) for m in pattern.finditer(text)]
    rest = pattern.sub(" ", text)
    return list(dict.fromkeys(tokens)), re.findall(r"(?<![A-Za-z-])\d{1,2}\b|(?<=[a-z])\d{1,2}\b", rest)


def _footnotes(page: str) -> dict[str, str]:
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", page))
    text = re.sub(r"\s+", " ", text)
    start = text.find("Footnotes 1 ")
    if start < 0:
        return {}
    text = text[start + len("Footnotes "):]
    end = text.find("Content current as of")
    text = text[:end if end > 0 else 6000]
    notes, n = {}, 1
    while True:
        here = re.match(rf"{n} (.*?)(?= {n + 1} [A-Z“\"(]|$)", text, re.S)
        if not here:
            break
        notes[str(n)] = here.group(1).strip()
        text = text[here.end():].lstrip()
        n += 1
    return notes


def main() -> None:
    if len(sys.argv) > 1:
        page = open(sys.argv[1], encoding="utf-8").read()
    else:
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
    from io import StringIO
    table = pd.read_html(StringIO(page))[0]
    as_of = re.search(r"Content current as of:\s*(?:<[^>]+>\s*)*([\d/]+)", page)

    drugs = {}
    for _, row in table.iterrows():
        name, notes = _name_and_notes(str(row["Drug or Other Substance"]))
        entry = {"notes": notes}
        for col, (kind, level) in COLUMNS.items():
            value = row.get(col)
            if not isinstance(value, str) or not value.strip():
                continue
            pattern = TRANSPORTER_RE if kind.startswith("transporter") else ENZYME_RE
            tokens, cell_notes = _cell(value, pattern)
            # A note in a cell belongs to that cell's kind (rifampin's
            # "Single dose" is about its OATP1B inhibition, not its induction).
            if cell_notes:
                entry.setdefault("kind_notes", {}).setdefault(kind, [])
                entry["kind_notes"][kind] = sorted(set(entry["kind_notes"][kind] + cell_notes), key=int)
            if level is None:
                entry.setdefault(kind, [])
                entry[kind] += [t for t in tokens if t not in entry[kind]]
            else:
                for t in tokens:
                    entry.setdefault(kind, {})[t] = level
        entry["notes"] = sorted(set(entry["notes"]), key=int)
        drugs[name] = entry

    out = {
        "source": URL,
        "as_of": as_of.group(1) if as_of else None,
        "definitions": {
            "inhibitor": "strong: raises the AUC of sensitive substrates >=5-fold; moderate: >=2 to <5-fold; "
                         "weak: >=1.25 to <2-fold",
            "inducer": "strong: lowers the AUC of sensitive substrates by >=80%; moderate: >=50 to <80%; "
                       "weak: >=20 to <50%",
            "substrate": "sensitive: AUC rises >=5-fold with a strong inhibitor; moderate sensitive: >=2 to <5-fold",
        },
        "footnotes": _footnotes(page),
        "drugs": drugs,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"{len(drugs)} drugs, {len(out['footnotes'])} footnotes, as of {out['as_of']} -> {OUT}")


if __name__ == "__main__":
    main()
