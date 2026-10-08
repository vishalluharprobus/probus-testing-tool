"""
Write a quote matrix run to disk: a CSV for Excel and an HTML page to read.

    reports/matrix-<when>/results.xlsx   one row per journey x insurer,
                                         Success / Failure, coloured
    reports/matrix-<when>/results.csv    the same rows, plain
    reports/matrix-<when>/report.html    the grid, the findings, the rules
    reports/matrix-<when>/findings.txt   the findings as plain text
(cars: reports/matrix-car-<when>/...)

Both are built from the same rows the terminal printed, so the three never
disagree.
"""
from __future__ import annotations

import csv
import html
from pathlib import Path

from core import matrixexcel
from data import matrix
from data.labscenarios import PRODUCTS

# One letter per outcome, used in the terminal grid and the HTML alike.
LEGEND = {
    "Y": "priced",
    "R": "refused by OUR rules (Probus declined before asking)",
    "N": "the insurer said no",
    "S": "'same insurer' - the previous insurer cannot renew itself (expected)",
    "D": "the insurer's service was down or timed out",
    "B": "failed in OUR code (null value, core service, HTTP error)",
    "E": "empty answer - the page shows NOTHING for this insurer",
    "?": "asked, never answered",
    ".": "not asked in this journey",
}


def cell(journey, insurer: str) -> str:
    if insurer in journey.offers:
        return "Y"
    if insurer in journey.silent:
        return "?"
    kind = journey.kinds.get(insurer)
    if kind is None:
        return "?" if insurer in journey.silent else "."
    reason = next((d.reason.lower() for d in journey.declines
                   if d.insurer == insurer), "")
    if matrix.says_same_insurer(reason):
        return "S"
    if "empty answer" in reason:
        return "E"
    return {"probus-rule": "R", "insurer-down": "D", "http": "B",
            "our-defect": "B"}.get(kind, "N")


def write(folder: Path, journeys: list, insurers: list[str], findings: list,
          relations: dict, rules: list, changes: list, facts: dict,
          vehicles: dict, product: str = "bike",
          company_ids: dict | None = None) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    paths = {"csv": folder / "results.csv", "html": folder / "report.html",
             "txt": folder / "findings.txt", "xlsx": folder / "results.xlsx"}
    _csv(paths["csv"], journeys)
    _txt(paths["txt"], findings, relations, rules, changes)
    title = f"Quote matrix - {PRODUCTS[product].title}"
    _html(paths["html"], journeys, insurers, findings, relations, rules,
          changes, facts, vehicles, title)
    rows = matrixexcel.rows_for(journeys, findings, vehicles, product, company_ids)
    ran = sum(1 for j in journeys if j.status in ("ok", "incomplete"))
    subtitle = (f"{ran} of {len(journeys)} journeys reached the quotes · "
                f"{len(insurers)} insurers · quotes only, nothing bought")
    try:
        if not matrixexcel.write(paths["xlsx"], title, subtitle, rows, findings,
                                 relations):
            paths.pop("xlsx")              # no openpyxl - the CSV stands
    except PermissionError:
        paths.pop("xlsx")                  # open in Excel right now
    return paths


def _csv(path: Path, journeys: list) -> None:
    head = (["journey", "kind", "why", "status"] + list(matrix.DIMENSIONS)
            + ["insurer", "outcome", "premium", "net", "gst", "od_part",
               "tp_part", "pa_cover", "ncb_percent", "ncb_discount", "idv",
               "idv_min", "idv_max", "seconds", "reason", "decided_by",
               "quotation"])
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        out = csv.writer(fh)
        out.writerow(head)
        for j in journeys:
            base = [j.n, j.scenario.kind, j.scenario.why, j.status] + \
                ["" if v is None else v for v in j.scenario.values]
            if not j.offers and not j.declines:
                out.writerow(base + ["", j.status, "", "", "", "", "", "", "",
                                     "", "", "", "", "", j.note, "", j.quotation])
                continue
            for code, a in sorted(j.offers.items()):
                out.writerow(base + [code, "Success", a.premium, a.net, a.gst,
                                     a.od, a.tp, a.pa_cover, a.ncb_percent,
                                     a.ncb_discount, a.idv, a.idv_min, a.idv_max,
                                     a.seconds, "", "", j.quotation])
            for d in j.declines:
                out.writerow(base + [d.insurer, "Failure"] + [""] * 12
                             + [d.reason, j.kinds.get(d.insurer, d.source),
                                j.quotation])


def _grouped(findings: list) -> list[tuple]:
    """One line per (severity, check, insurer), with every journey it hit."""
    groups: dict[tuple, list] = {}
    for f in findings:
        groups.setdefault((f.severity, f.check, f.insurer), []).append(f)
    order = {"DEFECT": 0, "LOOK": 1}
    return sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 2),
                                                  kv[0][2], kv[0][1]))


def grouped_lines(findings: list) -> list[tuple[str, str, str]]:
    """(severity, one line, detail) per group - shared with the terminal."""
    out = []
    for (severity, _check, insurer), items in _grouped(findings):
        journeys = sorted({n for f in items for n in f.journeys})
        where = ", ".join(f"#{n}" for n in journeys)
        more = f" (+{len(items) - 1} more like it)" if len(items) > 1 else ""
        out.append((severity, f"{insurer:<12} {items[0].title}{more}  [{where}]",
                    items[0].detail))
    return out


def _txt(path: Path, findings, relations, rules, changes) -> None:
    lines = []
    for severity, line, detail in grouped_lines(findings):
        lines.append(f"{severity:<7} {line}")
        if detail:
            lines.append(f"        {detail}")
    lines += ["", "TWIN RULES"]
    lines += [f"  {relations.get(r, 'not checked'):<12} {text}"
              for r, text in matrix.RELATIONS.items()]
    lines += ["", "LEARNED"] + [f"  {c:<12} {t}" for c, t in rules]
    lines += ["", "CHANGED SINCE LAST TIME"] + [f"  {c}" for c in changes]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a64;--line:#e4e1d8;--card:#fff;
--y:#dff3e4;--yt:#17603a;--n:#f4f1ea;--nt:#6b6a64;--b:#fde2e0;--bt:#9b1c14;
--r:#e8eefc;--rt:#23408e;--d:#fff2d6;--dt:#8a5a00;--s:#efe9fb;--st:#5b3b9a}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#171716;
--fg:#ecebe6;--muted:#a3a19a;--line:#34332f;--card:#201f1d;--y:#173a26;--yt:#8fdcaa;
--n:#2a2926;--nt:#a3a19a;--b:#4a1c19;--bt:#ffb4ab;--r:#1e2a4a;--rt:#aac2ff;
--d:#3d2f10;--dt:#f5c86b;--s:#2d2440;--st:#cdb8f5}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;padding:24px 16px}
main{max-width:1180px;margin:0 auto}h1{font-size:24px;margin:0 0 4px}
h2{font-size:17px;margin:28px 0 8px}.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 16px;margin:10px 0;overflow-x:auto}
table{border-collapse:collapse;font-size:13px}th,td{padding:4px 6px;
border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
td.c{text-align:center;font-weight:600;min-width:34px;border-radius:4px}
.Y{background:var(--y);color:var(--yt)}.N,.Q{background:var(--n);color:var(--nt)}
.B{background:var(--b);color:var(--bt)}.R{background:var(--r);color:var(--rt)}
.D,.E{background:var(--d);color:var(--dt)}.S{background:var(--s);color:var(--st)}
.tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:12px;
font-weight:600}.DEFECT{background:var(--b);color:var(--bt)}
.LOOK{background:var(--d);color:var(--dt)}li{margin:4px 0}
.small{font-size:13px}
"""


def _html(path, journeys, insurers, findings, relations, rules, changes,
          facts, vehicles, heading: str = "Quote matrix") -> None:
    e = html.escape
    ran = [j for j in journeys if j.offers or j.declines]
    rows = []
    for code in insurers:
        cells = []
        for j in ran:
            c = cell(j, code)
            title = LEGEND[c]
            if c == "Y":
                a = j.offers[code]
                title = f"Rs {a.premium:,.0f} · IDV {a.idv or 0:,.0f}"
                text = f"{a.premium / 1000:.1f}k"
            else:
                reason = next((d.reason for d in j.declines if d.insurer == code), "")
                title = reason or title
                text = c
            cls = "Q" if c in ("?", ".") else c
            cells.append(f'<td class="c {cls}" title="{e(title)}">{e(text)}</td>')
        rows.append(f"<tr><th>{e(code)}</th>{''.join(cells)}</tr>")
    head = "".join(f'<th title="{e(j.scenario.short())}">#{j.n}</th>' for j in ran)

    journeys_html = "".join(
        f"<tr><td>#{j.n}</td><td>{e(j.scenario.kind)}</td>"
        f"<td>{e(j.scenario.short())}</td><td>{e(j.status)}</td>"
        f"<td>{len(j.offers)} priced / {len(j.declines)} no</td>"
        f"<td class='muted'>{e(j.scenario.why)}</td></tr>" for j in journeys)
    finding_html = "".join(
        f"<li><span class='tag {e(sev)}'>{e(sev)}</span> {e(line)}"
        + (f"<div class='muted small'>{e(detail)}</div>" if detail else "")
        + "</li>" for sev, line, detail in grouped_lines(findings)) \
        or "<li>No findings - every check passed.</li>"
    relation_html = "".join(
        f"<li><b>{e(relations.get(r, 'not checked'))}</b> - {e(t)}</li>"
        for r, t in matrix.RELATIONS.items())
    rules_html = "".join(f"<li><b>{e(c)}</b> {e(t)}</li>" for c, t in rules) \
        or "<li>Not enough evidence yet - rules appear after a few runs.</li>"
    change_html = "".join(f"<li>{e(c)}</li>" for c in changes) or \
        "<li>Nothing changed, or these journeys had not run before.</li>"
    legend = " ".join(f'<span class="tag {"Q" if k in "?." else k}">{e(k)}</span> '
                      f'{e(v)}&nbsp;&nbsp;' for k, v in LEGEND.items())
    cov = (f"This run tested {facts.get('this_run', 0)} of {facts.get('wanted', 0)} "
           f"pairs. Together with earlier runs: {facts.get('with_this_run', 0)} of "
           f"{facts.get('wanted', 0)}. A complete pass takes about "
           f"{facts.get('full_plan_size', '?')} journeys.")
    defects = sum(1 for f in findings if f.severity == "DEFECT")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quote Matrix</title><style>{CSS}</style></head><body><main>
<h1>{e(heading)}</h1>
<p class="muted">{len(ran)} journeys ran · {len(insurers)} insurers ·
{defects} defect(s) · {len(findings) - defects} to look at</p>
<h2>Who priced what</h2><div class="card"><table><tr><th></th>{head}</tr>
{''.join(rows)}</table><p class="small muted">{legend}</p></div>
<h2>Findings</h2><div class="card"><ul>{finding_html}</ul></div>
<h2>Twin rules</h2><div class="card"><ul>{relation_html}</ul></div>
<h2>What the evidence says</h2><div class="card"><ul>{rules_html}</ul></div>
<h2>Changed since last time</h2><div class="card"><ul>{change_html}</ul></div>
<h2>Coverage</h2><div class="card"><p>{e(cov)}</p></div>
<h2>The journeys</h2><div class="card"><table>{journeys_html}</table></div>
</main></body></html>"""
    path.write_text(page, encoding="utf-8")
