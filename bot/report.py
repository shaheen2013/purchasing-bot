"""runs/<run_id>/report.csv and report.html."""
from __future__ import annotations

import csv
import html
import os
from collections import defaultdict
from pathlib import Path

from . import models as M
from .engine import summarize
from .ledger import Ledger


def _history(ledger: Ledger, run_id: str) -> tuple[dict[int, list], dict[int, list]]:
    lines, batches = defaultdict(list), defaultdict(list)
    for e in ledger.events(run_id):
        target = lines if e["kind"] == "line" else batches if e["kind"] == "batch" else None
        if target is not None:
            target[e["ref"]].append(e)
    return lines, batches


def write_reports(ledger: Ledger, run_id: str, runs_dir: Path = Path("runs")) -> tuple[Path, Path]:
    out = runs_dir / run_id
    out.mkdir(parents=True, exist_ok=True)
    line_hist, batch_hist = _history(ledger, run_id)
    batches = {b["batch_no"]: b for b in ledger.batches(run_id)}

    csv_path = out / "report.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["line_no", "product_input", "prod_id", "title", "qty_requested", "qty_planned",
                    "max_unit_price", "price_live", "price_seen", "decision", "batch_no", "state",
                    "order_id", "error", "state_history"])
        for ln in ledger.lines(run_id):
            b = batches.get(ln["batch_no"])
            hist = " > ".join(f"{e['to_state']}@{e['ts'][11:19]}" for e in line_hist[ln["line_no"]])
            w.writerow([ln["line_no"], ln["product_input"], ln["prod_id"], ln["title"], ln["qty_requested"],
                        ln["qty_planned"], ln["max_unit_price"], ln["price_live"], ln["price_seen"],
                        ln["decision"], ln["batch_no"], ln["state"], b["order_id"] if b else "",
                        ln["error"], hist])

    html_path = out / "report.html"
    html_path.write_text(_render_html(ledger, run_id, line_hist, batch_hist, out), encoding="utf-8")
    return csv_path, html_path


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _render_html(ledger, run_id, line_hist, batch_hist, out_dir: Path) -> str:
    run = ledger.get_run(run_id)
    s = summarize(ledger, run_id)
    live = bool(run["live"])
    cards = [("Ordered units", s["ordered_units"]), ("Confirmed batches", f"{s['batches_confirmed']} / {s['batches']}"),
             ("Skipped lines", s["skipped"]), ("Failed / uncertain lines", s["failed"]),
             ("Spend (confirmed)", f"${s['spend']:,}"), ("Planned spend", f"${s['planned_spend']:,}")]
    card_html = "".join(f'<div class="card"><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div></div>'
                        for k, v in cards)

    brow = []
    for b in ledger.batches(run_id):
        shot = ""
        if b["screenshot"]:
            rel = os.path.relpath(b["screenshot"], out_dir).replace("\\", "/")
            shot = f'<a href="{_e(rel)}">screenshot</a>'
        hist = " &rarr; ".join(_e(e["to_state"]) for e in batch_hist[b["batch_no"]])
        brow.append(
            f"<tr><td>{b['batch_no']}</td><td><span class='st {_e(b['state'])}'>{_e(b['state'])}</span></td>"
            f"<td>{_e(b['order_id'])}</td><td>{_e(b['amount'])}</td><td>{_e(b['expected_total'])}</td>"
            f"<td class='nw'>{'**** ' + _e(b['card_last4']) if b['card_last4'] else ''}</td><td>{_e(b['name'])}</td>"
            f"<td class='nw'>{_e(b['order_date'] or b['order_date_raw'])}</td><td>{_e(b['purchase_attempts'])}</td>"
            f"<td>{shot}</td><td class='small'>{hist}<br>{_e(b['error'])}</td></tr>")

    lrow = []
    for ln in ledger.lines(run_id):
        hist = "".join(f"<li><code>{_e(e['ts'][11:19])}</code> {_e(e['to_state'])}"
                       f"{': ' + _e(e['note']) if e['note'] else ''}</li>" for e in line_hist[ln["line_no"]])
        lrow.append(
            f"<tr><td>{ln['line_no']}</td><td>{_e(ln['product_input'])}</td><td>{_e(ln['title'])}</td>"
            f"<td>{_e(ln['qty_planned'] or 0)}/{_e(ln['qty_requested'] or ln['qty_input'])}</td>"
            f"<td>{_e(ln['price_seen'])}</td><td>{_e(ln['max_unit_price'] or ln['max_price_input'])}</td>"
            f"<td>{_e(ln['decision'])}</td><td>{_e(ln['batch_no'])}</td>"
            f"<td><span class='st {_e(ln['state'])}'>{_e(ln['state'])}</span></td><td>{_e(ln['error'])}</td>"
            f"<td><details><summary>history</summary><ul>{hist}</ul></details></td></tr>")

    banner = ("" if live else "<p class='dry'>DRY RUN: the cart was built and reconciled, "
                              "but Purchase was never clicked.</p>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bulk Purchase Report</title>
<style>
:root {{ --bg:#f7f8fa; --fg:#1d2330; --muted:#667085; --card:#fff; --line:#e4e7ec; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --fg:#e6e8eb; --muted:#9aa3b2; --card:#1d2128; --line:#2c323c; }} }}
body {{ background:var(--bg); color:var(--fg); font:14px/1.45 system-ui,Segoe UI,sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:1200px; margin:0 auto; }}
h1 {{ margin:0 0 4px; font-size:22px; }} .muted {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin:18px 0; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px; }}
.k {{ color:var(--muted); font-size:12px; }} .v {{ font-size:22px; font-weight:600; }}
.wrap {{ overflow-x:auto; background:var(--card); border:1px solid var(--line); border-radius:8px; margin-bottom:20px; }}
table {{ border-collapse:collapse; width:100%; }} th,td {{ text-align:left; padding:7px 9px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ font-size:12px; color:var(--muted); font-weight:600; }} .small {{ font-size:12px; color:var(--muted); }} .nw {{ white-space:nowrap; }}
.st {{ padding:1px 7px; border-radius:10px; font-size:12px; font-weight:600; background:#8881; }}
.CONFIRMED {{ color:#12805c; }} .CONFIRMED_UNVERIFIED {{ color:#b26b00; }} .SKIPPED {{ color:#9a6b00; }}
.FAILED {{ color:#c4320a; }} .UNCERTAIN {{ color:#a11ea8; }} .RECONCILED {{ color:#0b6aa2; }}
.dry {{ background:#fff4d6; color:#5c4400; padding:10px 12px; border-radius:8px; }}
ul {{ margin:4px 0; padding-left:18px; }} code {{ font-size:12px; }}
</style></head><body><main>
<h1>Bulk purchase report</h1>
<div class="muted">Run <b>{_e(run_id)}</b> &middot; mode {_e(run['mode'])} &middot; {'LIVE' if live else 'DRY RUN'}
 &middot; status <b>{_e(run['status'])}</b> &middot; orders {_e(run['orders_file'])} &middot; created {_e(run['created_at'])}</div>
{banner}
<div class="cards">{card_html}</div>
<h2>Batches</h2><div class="wrap"><table><tr><th>#</th><th>State</th><th>Order id</th><th>Amount</th><th>Expected</th>
<th>Card</th><th>Name</th><th>Date</th><th>Attempts</th><th>Evidence</th><th>History / error</th></tr>{''.join(brow)}</table></div>
<h2>Lines</h2><div class="wrap"><table><tr><th>#</th><th>Input</th><th>Product</th><th>Qty</th><th>Price seen</th><th>Max</th>
<th>Decision</th><th>Batch</th><th>State</th><th>Note</th><th>History</th></tr>{''.join(lrow)}</table></div>
<p class="small">Stock levels are SIMULATED (Demoblaze has no inventory). Card numbers are test numbers, stored masked to the last 4 digits.</p>
</main></body></html>"""
