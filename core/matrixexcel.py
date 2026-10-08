"""
The quote matrix's spreadsheet: one row per journey and insurer, coloured.

    Results   #, company id, company, sub product, segment, scenario, journey
              type, then Status (Success / Failure / Not run) with its reason,
              who decided, notes ("Bug:" / "Worth a look:") and the figures
    Summary   what ran, how each insurer did, the findings, the twin rules

Same look as the insurer lab's sheet (core/labexcel.py) so the two read
alike. Needs openpyxl; without it write() returns False and the CSV stands.
"""
from __future__ import annotations

from pathlib import Path

from core.labexcel import (AMBER, AMBER_TEXT, BAND, GREEN, GREEN_TEXT, GREY,
                           GREY_TEXT, LINE, NAVY, RED, RED_TEXT, Workbook, _paint)
from data import matrix
from data.labscenarios import PRODUCTS, SEGMENTS

try:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ImportError:                 # pragma: no cover - reported by write()
    pass

SUCCESS, FAILURE, NOT_RUN = "Success", "Failure", "Not run"
LOOK = {SUCCESS: (GREEN, GREEN_TEXT), FAILURE: (RED, RED_TEXT),
        NOT_RUN: (GREY, GREY_TEXT)}

# Plain words for the planner's journey kinds.
JOURNEY_TYPE = {"baseline": "Standard quote",
                "twin": "Standard quote with one change",
                "pairwise": "Combination",
                "retest": "Re-check of a finding"}

# Who said no, in plain words (Decline.source / failure kind).
DECIDED_BY = {"probus-rule": "Our rules (Probus)", "http": "Our API",
              "our-defect": "Our integration", "silent": "No answer",
              "insurer-down": "Insurer (service down)"}

COLUMNS = (  # heading, key, width, kind
    ("#", "n", 5, "int"), ("Company ID", "company_id", 11, "text"),
    ("Company", "company", 13, "text"), ("Sub Product", "sub_product", 19, "text"),
    ("Segment", "segment", 18, "text"), ("Scenario", "scenario", 58, "wrap"),
    ("Journey Type", "journey_type", 20, "wrap"), ("Status", "status", 11, "status"),
    ("Reason", "reason", 56, "wrap"), ("Decided By", "decided_by", 18, "text"),
    ("Notes", "notes", 46, "wrap"), ("Premium (Rs)", "premium", 13, "money"),
    ("OD (Rs)", "od", 11, "money"), ("TP (Rs)", "tp", 11, "money"),
    ("GST (Rs)", "gst", 11, "money"), ("NCB %", "ncb", 8, "int"),
    ("IDV (Rs)", "idv", 12, "money"), ("Quotation No.", "quotation", 30, "text"),
    ("Time (s)", "seconds", 9, "secs"),
)
HEADER_ROW = 4


def rows_for(journeys: list, findings: list, vehicles: dict, product: str,
             company_ids: dict | None = None) -> list[dict]:
    """The sheet's rows - kept apart from the drawing so tests can read them."""
    info = PRODUCTS[product]
    company_ids = company_ids or {}
    notes: dict[tuple, list[str]] = {}
    for f in findings:
        for n in f.journeys:
            notes.setdefault((n, f.insurer), []).append(
                f"{'Bug' if f.severity == 'DEFECT' else 'Worth a look'}: {f.title}")
    def company_id(code: str) -> str:
        if code in company_ids:
            return str(company_ids[code] or "")
        near = next((v for k, v in company_ids.items() if k and (k in code or code in k)),
                    "")
        return str(near or "")

    out = []
    for j in journeys:
        s = j.scenario
        policy = s.get("policy")
        base = {"n": j.n, "sub_product": f"{info.sub_product} - {info.title}",
                "segment": f"{SEGMENTS.get(policy, '?')} - {policy}",
                "scenario": s.short(vehicles),
                "journey_type": JOURNEY_TYPE.get(s.kind, s.kind),
                "quotation": j.quotation}
        if j.status not in ("ok", "incomplete"):
            form = notes.get((j.n, "FORM"), [])
            out.append({**base, "company": "-",
                        "status": NOT_RUN if j.status == "skipped" else FAILURE,
                        "reason": f"Journey did not reach the quotes: {j.note}",
                        "decided_by": "The form" if j.status in ("blocked", "form",
                                                                 "skipped")
                        else "Environment" if j.status == "environment" else "-",
                        "notes": "; ".join(form), "seconds": j.seconds})
            continue
        for code, a in sorted(j.offers.items()):
            out.append({**base, "company": code, "company_id": company_id(code),
                        "status": SUCCESS, "reason": "", "decided_by": "",
                        "notes": "; ".join(notes.get((j.n, code), [])),
                        "premium": a.premium, "od": a.od, "tp": a.tp, "gst": a.gst,
                        "ncb": a.ncb_percent, "idv": a.idv, "seconds": a.seconds})
        for d in j.declines:
            kind = j.kinds.get(d.insurer, d.source)
            reason = d.reason
            if matrix.says_same_insurer(reason):
                reason += " (expected - the previous insurer cannot renew itself)"
            elif "enter vehicle registration number" in reason.lower():
                # InsureBridge TwoWheelerAgent.cs: TATA (any renewal) and BAJAJ
                # (Comprehensive/OD renewals) refuse the XX-XX-AB-1111 number
                # the "don't know my number" journey sends.
                reason += (" (expected - this insurer needs the real registration "
                           "number for a renewal; this journey used 'I don't know "
                           "my number')")
            out.append({**base, "company": d.insurer,
                        "company_id": company_id(d.insurer),
                        "status": FAILURE, "reason": reason,
                        "decided_by": DECIDED_BY.get(kind, "Insurer"),
                        "notes": "; ".join(notes.get((j.n, d.insurer), []))})
    return out


def write(path: Path, title: str, subtitle: str, rows: list[dict],
          findings: list, relations: dict) -> bool:
    if Workbook is None:
        return False
    book = Workbook()
    _results(book.active, title, subtitle, rows)
    _summary(book.create_sheet("Summary"), title, rows, findings, relations)
    book.save(path)
    return True


def _results(ws, title: str, subtitle: str, rows: list[dict]) -> None:
    ws.title = "Results"
    ws.sheet_view.showGridLines = False
    ws["A1"] = title
    ws["A1"].font = Font(size=16, bold=True, color=NAVY)
    ws["A2"] = subtitle
    ws["A2"].font = Font(italic=True, color="595959")
    thin = Side(style="thin", color=LINE)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for i, (heading, _, width, _) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=HEADER_ROW, column=i, value=heading)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[HEADER_ROW].height = 30
    for r, row in enumerate(rows, start=HEADER_ROW + 1):
        band = PatternFill("solid", fgColor=BAND) if r % 2 == 0 else None
        for i, (_, key, _, kind) in enumerate(COLUMNS, start=1):
            value = row.get(key)
            cell = ws.cell(row=r, column=i, value=None if value in ("", None) else value)
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=kind == "wrap",
                                       horizontal="center" if kind in
                                       ("status", "int") else None)
            if band is not None:
                cell.fill = band
            if kind == "money":
                cell.number_format = "#,##0"
            elif kind == "secs":
                cell.number_format = "0.0"
            elif kind == "status" and value in LOOK:
                _paint(cell, *LOOK[value], bold=True)
            elif key == "reason" and value:
                cell.font = Font(color=RED_TEXT)
            elif key == "notes" and value:
                bug = "Bug:" in str(value)
                _paint(cell, AMBER, RED_TEXT if bug else AMBER_TEXT, bold=bug)
    last = HEADER_ROW + max(1, len(rows))
    ws.auto_filter.ref = f"A{HEADER_ROW}:{get_column_letter(len(COLUMNS))}{last}"
    ws.freeze_panes = f"D{HEADER_ROW + 1}"


def _summary(ws, title: str, rows: list[dict], findings: list,
             relations: dict) -> None:
    ws.sheet_view.showGridLines = False
    for col, width in zip("ABCDE", (22, 14, 11, 11, 70)):
        ws.column_dimensions[col].width = width
    ws["A1"] = f"{title} - summary"
    ws["A1"].font = Font(size=16, bold=True, color=NAVY)

    r = 3
    ws.cell(row=r, column=1, value="How each insurer did").font = Font(
        size=13, bold=True, color=NAVY)
    r += 1
    for i, head in enumerate(("Company", "Company ID", SUCCESS, FAILURE,
                              "Most common reason for Failure"), start=1):
        cell = ws.cell(row=r, column=i, value=head)
        if head in LOOK:
            _paint(cell, *LOOK[head], bold=True)
        else:
            cell.font = Font(bold=True)
    by: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("company") not in (None, "", "-"):
            by.setdefault(row["company"], []).append(row)
    for code, mine in sorted(by.items(), key=lambda kv: (-sum(
            x["status"] == SUCCESS for x in kv[1]), kv[0])):
        r += 1
        failed = [x["reason"] for x in mine if x["status"] == FAILURE and x["reason"]]
        common = max(set(failed), key=failed.count) if failed else ""
        values = (code, mine[0].get("company_id") or "",
                  sum(x["status"] == SUCCESS for x in mine), len(failed), common)
        for i, value in enumerate(values, start=1):
            cell = ws.cell(row=r, column=i, value=value)
            cell.alignment = Alignment(wrap_text=i == 5, vertical="top",
                                       horizontal="center" if i in (3, 4) else None)

    r += 2
    ws.cell(row=r, column=1, value="What the checks found").font = Font(
        size=13, bold=True, color=NAVY)
    seen = set()
    for f in sorted(findings, key=lambda f: (f.severity != "DEFECT", f.insurer)):
        line = (f.severity, f.insurer, f.title)
        if line in seen:
            continue
        seen.add(line)
        r += 1
        bug = f.severity == "DEFECT"
        ws.cell(row=r, column=1, value="Bug" if bug else "Worth a look").font = Font(
            bold=True, color=RED_TEXT if bug else AMBER_TEXT)
        ws.cell(row=r, column=2, value=f.insurer)
        cell = ws.cell(row=r, column=5, value=f"{f.title}  [journeys "
                       f"{', '.join(f'#{n}' for n in f.journeys)}]")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    if not seen:
        r += 1
        ws.cell(row=r, column=1, value="Nothing - every check passed.").font = Font(
            bold=True, color=GREEN_TEXT)

    r += 2
    ws.cell(row=r, column=1, value="Rules checked by comparing two quotes").font = Font(
        size=13, bold=True, color=NAVY)
    plain = {"held": ("Held", GREEN, GREEN_TEXT), "broken": ("Broken", RED, RED_TEXT),
             "not checked": ("Not checked", GREY, GREY_TEXT)}
    for relation, text in matrix.RELATIONS.items():
        r += 1
        word, fill, ink = plain[relations.get(relation, "not checked")]
        cell = ws.cell(row=r, column=1, value=word)
        _paint(cell, fill, ink, bold=True)
        ws.cell(row=r, column=5, value=text).alignment = Alignment(wrap_text=True)
