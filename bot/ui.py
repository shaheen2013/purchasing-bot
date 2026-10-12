"""Rich live dashboard: a log stream above a table of lines and batches."""
from __future__ import annotations

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import models as M

STATE_STYLE = {
    M.CONFIRMED: "bold green", M.CONFIRMED_UNVERIFIED: "yellow", M.SKIPPED: "dim yellow",
    M.FAILED: "bold red", M.UNCERTAIN: "bold magenta", M.CHECKOUT_SENT: "bold cyan",
    M.RECONCILED: "green", M.IN_CART: "cyan", M.VALIDATED: "blue", M.PENDING: "dim",
}


class ConsoleUI:
    def __init__(self, console: Console | None = None, show_api: bool = True):
        self.console = console or Console()
        self.show_api = show_api
        self.ledger = None
        self.run_id: str | None = None
        self.header = ""
        self._live: Live | None = None

    def attach(self, ledger, run_id: str, header: str) -> None:
        self.ledger, self.run_id, self.header = ledger, run_id, header

    def start(self) -> None:
        self._live = Live(self._render(), console=self.console, refresh_per_second=4, transient=False)
        self._live.start()

    def stop(self) -> None:
        if self._live:
            self._live.update(self._render())
            self._live.stop()
            self._live = None

    def log(self, msg: str, style: str | None = None) -> None:
        self.console.print(msg, style=style, highlight=False, markup=False)

    def api(self, path: str, info: str) -> None:
        if self.show_api:
            self.console.print(f"  [api] POST {path} -> {info}" if path != "/entries" else f"  [api] GET {path} -> {info}",
                               style="dim", highlight=False, markup=False)

    def refresh(self) -> None:
        if self._live:
            self._live.update(self._render())

    def _render(self):
        run = self.ledger.get_run(self.run_id) if self.ledger and self.run_id else None
        if run is None:
            return Panel("planning...")
        lines = Table(title="Order lines", expand=True, show_lines=False)
        for col, kw in (("#", {"justify": "right"}), ("Product", {}), ("Qty", {"justify": "right"}),
                        ("Price / Max", {"justify": "right"}), ("Decision", {}), ("Batch", {"justify": "right"}),
                        ("State", {}), ("Note", {"overflow": "fold", "ratio": 2})):
            lines.add_column(col, **kw)
        for ln in self.ledger.lines(self.run_id):
            qty = f"{ln['qty_planned'] or 0}/{ln['qty_requested'] or ln['qty_input']}"
            price = f"{ln['price_seen']:g} / {ln['max_unit_price']:g}" if ln["price_seen"] is not None else ""
            title = ln["title"] or ln["product_input"] or ""
            lines.add_row(str(ln["line_no"]), Text(title), qty, price, ln["decision"] or "",
                          str(ln["batch_no"] or ""), Text(ln["state"], style=STATE_STYLE.get(ln["state"], "")),
                          Text(ln["error"] or ""))
        batches = Table(title="Batches (one checkout each)", expand=True)
        for col in ("Batch", "State", "Units", "Total", "Attempts", "Order id", "Amount", "Card"):
            batches.add_column(col)
        for b in self.ledger.batches(self.run_id):
            units = self.ledger.units(self.run_id, b["batch_no"])
            batches.add_row(str(b["batch_no"]), Text(b["state"], style=STATE_STYLE.get(b["state"], "")),
                            str(len(units)), str(b["expected_total"] or ""), str(b["purchase_attempts"] or 0),
                            b["order_id"] or "", str(b["amount"] or ""),
                            f"****{b['card_last4']}" if b["card_last4"] else "")
        head = f"[bold]{self.header}[/]   run [cyan]{self.run_id}[/]   status [bold]{run['status']}[/]"
        return Panel(Group(lines, batches), title=head, border_style="blue")
