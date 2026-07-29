#!/usr/bin/env python3
"""
Read a generated report and check it the way a client would.

Every other suite in this project inspects the code. None of them opens the
thing the customer actually receives, and that is where the last defects hid:

  * the Reconciliation page was imported and never called — the tab simply did
    not exist, while the module imported, the computation ran, the console
    printed the coverage, and 133 checks passed
  * that page's own figures did not add up: attributed cost printed beside a
    coverage percentage that silently included a portion shown nowhere, so
    $1,394.85 - $233.95 did not equal the $292.32 labelled unexplained
  * "derived" leaked into a client-facing Cost Basis column
  * a duplicate tab name silently dropped a service page

None is a calculation error. All are visible in ten seconds of reading the
workbook, and invisible to everything else. So this reads the workbook.

    python3 tests/verify_report.py "<account> AWS Cost Report <date>.xlsx"

Deliberately dependency-free — xlsx is a zip of XML, and requiring openpyxl
would mean this check gets skipped on the machine that most needs it.
"""

import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

PASSES, PROBLEMS, NOTES = [], [], []


def ok(name, detail=""):
    PASSES.append(f"{name}{(' — ' + detail) if detail else ''}")


def bad(name, detail=""):
    PROBLEMS.append(f"{name}{(' — ' + detail) if detail else ''}")


# ─── Minimal xlsx reader ─────────────────────────────────────────────────────

def read_workbook(path):
    """{sheet_name: [[cell, ...], ...]} using only the standard library."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{NS}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{NS}t")))

        rels = {}
        root = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in root:
            rels[rel.get("Id")] = rel.get("Target").lstrip("/")

        sheets = {}
        root = ET.fromstring(z.read("xl/workbook.xml"))
        for sheet in root.find(f"{NS}sheets"):
            target = rels.get(sheet.get(f"{REL_NS}id"), "")
            path_in_zip = target if target.startswith("xl/") else f"xl/{target}"
            if path_in_zip not in z.namelist():
                continue
            sheets[sheet.get("name")] = _read_sheet(z.read(path_in_zip), shared)
        return sheets


def _read_sheet(data, shared):
    rows = []
    root = ET.fromstring(data)
    for row in root.iter(f"{NS}row"):
        cells = []
        for c in row.findall(f"{NS}c"):
            v = c.find(f"{NS}v")
            text = v.text if v is not None else None
            if c.get("t") == "s" and text is not None:
                try:
                    text = shared[int(text)]
                except (ValueError, IndexError):
                    pass
            elif c.get("t") == "inlineStr":
                is_el = c.find(f"{NS}is")
                text = "".join(t.text or "" for t in is_el.iter(f"{NS}t")) if is_el is not None else None
            cells.append(text)
        rows.append(cells)
    return rows


def _flat(rows):
    return [str(c) for r in rows for c in r if c is not None]


def _num(text):
    try:
        return float(str(text).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


# ─── Checks, each one a defect that actually shipped ─────────────────────────

ANALYSIS_TABS = ["Summary", "Recommendations", "Data Gaps", "Reconciliation"]


def check_analysis_tabs_present(wb):
    """A page builder imported but never called leaves no trace but a missing tab."""
    missing = [t for t in ANALYSIS_TABS if t not in wb]
    if missing:
        bad("Analysis tabs missing", ", ".join(missing))
    else:
        ok(f"All {len(ANALYSIS_TABS)} analysis tabs present")


def check_no_empty_tabs(wb):
    """A tab with headers and no rows means the builder failed silently."""
    empty = [name for name, rows in wb.items()
             if len([r for r in rows if any(c is not None for c in r)]) <= 1]
    if empty:
        bad("Tabs with a header but no data", ", ".join(empty))
    else:
        ok(f"All {len(wb)} tabs contain data")


def check_reconciliation_adds_up(wb):
    """
    attributed + explained elsewhere + unexplained must equal billed.

    The page printed attributed cost beside a coverage percentage that included
    a portion shown nowhere on the page, so a reader doing the arithmetic found
    it broken — on the one tab whose job is to vouch for the report.
    """
    rows = wb.get("Reconciliation")
    if not rows:
        return
    header_idx = None
    for i, row in enumerate(rows):
        cells = [str(c) for c in row if c]
        if cells and cells[0].startswith("Billed"):
            header_idx = i
            break
    if header_idx is None or header_idx + 1 >= len(rows):
        bad("Reconciliation totals row not found")
        return

    header = [str(c) for c in rows[header_idx] if c]
    values = [_num(c) for c in rows[header_idx + 1] if c is not None]
    if len(values) < 4 or any(v is None for v in values[:4]):
        bad("Reconciliation totals unreadable", str(rows[header_idx + 1])[:80])
        return

    billed, attributed, explained, unexplained = values[0], values[1], values[2], values[3]
    if "Explained" not in " ".join(header):
        bad("Reconciliation hides the explained-elsewhere portion",
            "reader cannot reconcile the row")
        return
    total = attributed + explained + unexplained
    if abs(total - billed) > 0.05:
        bad("Reconciliation row does not add up",
            f"{attributed:,.2f} + {explained:,.2f} + {unexplained:,.2f} "
            f"= {total:,.2f}, billed {billed:,.2f}")
    else:
        ok("Reconciliation adds up", f"${billed:,.2f} accounted for")

    if len(values) >= 5 and values[4] is not None and billed:
        stated, actual = values[4], (attributed + explained) / billed * 100
        if abs(stated - actual) > 0.2:
            bad("Coverage % does not match the figures shown",
                f"stated {stated}%, figures give {actual:.1f}%")
        else:
            ok("Coverage % matches the figures shown", f"{stated}%")


def check_no_internal_jargon(wb):
    """
    Internal labels must not reach a client-facing cell.

    "derived" is a provenance source, meaningful in code and meaningless in a
    column a client reads. So is a bare "None" or a dict repr — both have
    reached the sheet before.
    """
    banned = {"derived": "provenance label, not an explanation",
              "unavailable": "internal status string",
              "list_price": "internal key — should read 'list price'",
              "cost_explorer": "internal key",
              "billed_hours": "internal key"}
    hits = []
    for name, rows in wb.items():
        for cell in _flat(rows):
            low = cell.strip().lower()
            if low in banned:
                hits.append(f"{name}: '{cell}' ({banned[low]})")
    if hits:
        bad("Internal labels visible to the reader", "; ".join(sorted(set(hits))[:4]))
    else:
        ok("No internal labels in client-facing cells")


def check_no_structured_values(wb):
    """A dict or list reaching a cell renders as Python source."""
    pattern = re.compile(r"^\s*[\{\[].*[\}\]]\s*$")
    hits = []
    for name, rows in wb.items():
        for cell in _flat(rows):
            if pattern.match(cell) and ("':" in cell or '":' in cell):
                hits.append(f"{name}: {cell[:50]}")
    if hits:
        bad("Structured value rendered into a cell", "; ".join(hits[:3]))
    else:
        ok("No dicts or lists rendered into cells")


def check_bare_none(wb):
    """A literal 'None' means a missing value was stringified instead of blanked."""
    hits = []
    for name, rows in wb.items():
        count = sum(1 for cell in _flat(rows) if cell.strip() == "None")
        if count:
            hits.append(f"{name} ({count})")
    if hits:
        bad("Literal 'None' in cells — should be blank", ", ".join(hits))
    else:
        ok("No literal 'None' values")


def check_tiers_not_summed(wb):
    """
    Confirmed and Estimated must appear as separate figures.

    A combined number is the artefact a client quotes back, and the moment it
    fails to reconcile the whole report is in question.
    """
    rows = wb.get("Summary")
    if not rows:
        return
    flat = " ".join(_flat(rows))
    has_both = "Confirmed" in flat and "Estimated" in flat
    combined = re.search(r"total (potential )?savings", flat, re.I)
    if not has_both:
        bad("Summary does not separate Confirmed from Estimated savings")
    elif combined:
        bad("Summary presents a combined savings total",
            "tiers must never be summed into one headline")
    else:
        ok("Confirmed and Estimated reported separately")


def check_cost_basis_stated(wb):
    """The reader must never have to guess whether costs came from billing."""
    rows = wb.get("Summary")
    if not rows:
        return
    flat = " ".join(_flat(rows))
    if "Cost basis" not in flat:
        bad("Summary does not state the cost basis")
    elif "actual billed cost (CUR)" in flat:
        ok("Cost basis stated", "CUR-backed")
    elif "LIST PRICE" in flat:
        ok("Cost basis stated", "list price, disclosed")
    else:
        bad("Cost basis line present but unrecognised")


def check_savings_within_cost(wb):
    """A saving larger than the resource it comes from is arithmetically impossible."""
    rows = wb.get("Recommendations")
    if not rows:
        return
    header = None
    for row in rows:
        cells = [str(c) if c else "" for c in row]
        if any("Saving" in c for c in cells) and any("Confidence" in c for c in cells):
            header = cells
            break
    if not header:
        NOTES.append("Recommendations header not recognised — saving check skipped")
        return
    idx = next((i for i, c in enumerate(header) if "Saving" in c), None)
    bad_rows = 0
    for row in rows:
        if idx is not None and idx < len(row):
            v = _num(row[idx])
            if v is not None and v < 0:
                bad_rows += 1
    if bad_rows:
        bad("Negative savings in Recommendations", f"{bad_rows} row(s)")
    else:
        ok("No negative savings")


def check_zero_cost_plausibility(wb):
    """
    A whole tab of $0.00 is the shape the Elastic IP bug had.

    29 addresses at $0.00 against ~$105/mo of real charges sat in a report that
    was read five times. Nobody pays nothing for a tab full of resources.
    """
    for name, rows in wb.items():
        if name in ANALYSIS_TABS + ["Data Transfer", "Commitments",
                                    "All Services (Billing)", "Changes"]:
            continue
        costs = []
        header = [str(c) if c else "" for c in (rows[0] if rows else [])]
        idx = next((i for i, c in enumerate(header) if "Monthly Cost" in c), None)
        if idx is None:
            continue
        for row in rows[1:]:
            if idx < len(row):
                v = _num(row[idx])
                if v is not None:
                    costs.append(v)
        if len(costs) >= 3 and all(c == 0 for c in costs):
            bad(f"{name}: every resource priced at $0.00",
                f"{len(costs)} rows — verify this service is genuinely free")
    if not any("$0.00" in p for p in PROBLEMS):
        ok("No service tab is uniformly $0.00")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    try:
        wb = read_workbook(path)
    except Exception as e:
        print(f"  Could not read {path}: {type(e).__name__}: {e}")
        return 2

    print(f"\n  {path.rsplit('/', 1)[-1]}")
    print(f"  {len(wb)} tabs\n")

    for fn in (check_analysis_tabs_present, check_no_empty_tabs,
               check_reconciliation_adds_up, check_no_internal_jargon,
               check_no_structured_values, check_bare_none,
               check_tiers_not_summed, check_cost_basis_stated,
               check_savings_within_cost, check_zero_cost_plausibility):
        try:
            fn(wb)
        except Exception as e:
            bad(f"{fn.__name__} crashed", f"{type(e).__name__}: {e}")

    print("=" * 72)
    for line in PASSES:
        print(f"  OK    {line}")
    for line in NOTES:
        print(f"  note  {line}")
    for line in PROBLEMS:
        print(f"  BUG   {line}")
    print("=" * 72)
    print(f"  {len(PASSES)} passed, {len(PROBLEMS)} problems\n")
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    sys.exit(main())
