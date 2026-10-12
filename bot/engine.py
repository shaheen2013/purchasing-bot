"""The purchasing engine: plan -> build cart -> reconcile -> (dry-run stop | checkout).

Safety rules implemented here:
  * cart building is idempotent: uuids are stored before adding, /viewcart is read first
    and only missing uuids are added;
  * reconciliation before paying: expected uuids present, no strays, totals equal #totalp;
  * Purchase is clicked at most once per attempt and never again blindly: after a
    post-click failure the cart is inspected first (empty = bought);
  * a batch that is CONFIRMED is never processed again.
"""
from __future__ import annotations

import math
import time
import uuid as uuidlib
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from . import models as M
from .browser import CheckoutError, parse_confirmation
from .config import CheckoutDetails
from .ledger import Ledger
from .orders import Catalog, StockBook, load_stock, read_orders, validate_row
from .simulate import Simulation

MAX_PURCHASE_ATTEMPTS = 2  # the original click + one retry when the cart proves nothing was bought


class AbortRun(Exception):
    pass


class Shop(Protocol):
    assigns_uuids: bool
    def catalog(self) -> list[M.Product]: ...
    def live_price(self, prod_id: int) -> float: ...
    def cart_items(self) -> list[M.CartItem]: ...
    def add_unit(self, unit_uuid: str | None, prod_id: int) -> str: ...
    def remove_item(self, unit_uuid: str) -> None: ...
    def clear_cart(self) -> None: ...


class Checkout(Protocol):
    def read_cart_page(self) -> M.CartPage: ...
    def purchase(self, details: CheckoutDetails, before_click: Callable[[], None],
                 simulate_timeout: bool = False) -> tuple[str, str | None]: ...
    def screenshot(self, name: str) -> str | None: ...


class NullUI:
    def log(self, msg: str, style: str | None = None) -> None: ...
    def refresh(self) -> None: ...


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuidlib.uuid4().hex[:4]


class Engine:
    def __init__(self, ledger: Ledger, shop: Shop, checkout: Checkout, details: CheckoutDetails, ui=None, *,
                 live: bool = False, strict: bool = False, sim: Simulation | None = None,
                 settle_polls: int = 5, settle_interval: float = 2.0, sleep: Callable[[float], None] = time.sleep):
        self.ledger, self.shop, self.checkout, self.details = ledger, shop, checkout, details
        self.ui = ui or NullUI()
        self.live, self.strict = live, strict
        self.sim = sim or Simulation()
        self.settle_polls, self.settle_interval, self.sleep = settle_polls, settle_interval, sleep

    def log(self, msg: str, style: str | None = None) -> None:
        self.ui.log(msg, style)
        self.ui.refresh()

    # =========================================================================
    # Planning
    # =========================================================================
    def plan(self, run_id: str, orders_path: Path, *, mode: str, batch_size: int = 0,
             allow_partial: bool = False, stock_path: Path | None = None) -> None:
        rows = read_orders(orders_path)
        self.ledger.create_run(run_id, mode, self.live, str(orders_path), batch_size,
                               {"allow_partial": allow_partial, "strict": self.strict,
                                "stock": str(stock_path) if stock_path else None,
                                "simulate": self.sim.describe()})
        self.log(f"Loaded {len(rows)} order line(s) from {orders_path}")
        catalog = Catalog(self.shop.catalog())
        self.log(f"Catalog: {len(catalog)} products")
        stock = StockBook(load_stock(stock_path), self.sim.stock_out)
        if stock.active:
            self.log("Stock guard active. NOTE: this inventory is SIMULATED (Demoblaze has no stock).", "yellow")
        for note in self.sim.describe():
            self.log(f"--simulate {note}", "magenta")

        price_cache: dict[int, float] = {}
        buyable: list[int] = []
        for row in rows:
            self.ledger.add_line(run_id, row.line_no, product_input=row.product, qty_input=row.qty,
                                 max_price_input=row.max_unit_price)
            v = validate_row(row, catalog)
            if v.errors:
                msg = "; ".join(v.errors)
                self.ledger.set_line_state(run_id, row.line_no, M.SKIPPED, msg, decision=M.REJECTED, error=msg,
                                           prod_id=v.product.id if v.product else None,
                                           title=v.product.title if v.product else None)
                self.log(f"line {row.line_no}: REJECTED: {msg}", "red")
                continue
            p = v.product
            self.ledger.set_line_state(run_id, row.line_no, M.VALIDATED, f"matched #{p.id} {p.title}",
                                       prod_id=p.id, title=p.title, qty_requested=v.qty,
                                       max_unit_price=v.max_unit_price)

            if p.id not in price_cache:
                price_cache[p.id] = self.shop.live_price(p.id)
            live_price = price_cache[p.id]
            seen = self.sim.observed_price(p.id, live_price)
            self.ledger.update_line(run_id, row.line_no, price_live=live_price, price_seen=seen)
            drift = f" (live {live_price:g}, simulated drift)" if seen != live_price else ""
            if seen > v.max_unit_price:
                msg = f"price {seen:g}{drift} > max {v.max_unit_price:g}"
                self.ledger.set_line_state(run_id, row.line_no, M.SKIPPED, msg, decision=M.SKIP_PRICE, error=msg)
                self.log(f"line {row.line_no}: SKIP_PRICE, {p.title}: {msg}", "yellow")
                continue

            decision, qty, note = stock.allocate(p.id, v.qty, allow_partial)
            if decision == M.SKIP_STOCK:
                hint = "" if allow_partial else " (use --allow-partial to buy what is left)"
                self.ledger.set_line_state(run_id, row.line_no, M.SKIPPED, note, decision=decision,
                                           error=note + hint)
                self.log(f"line {row.line_no}: SKIP_STOCK, {p.title}: {note}{hint}", "yellow")
                continue
            self.ledger.update_line(run_id, row.line_no, decision=decision, qty_planned=qty,
                                    error=note if decision == M.PARTIAL else None)
            self.log(f"line {row.line_no}: {decision} {qty} x {p.title} @ {seen:g} (max {v.max_unit_price:g})"
                     + (f" ({note})" if note else ""), "green")
            buyable.append(row.line_no)

        size = batch_size if batch_size and batch_size > 0 else max(len(buyable), 1)
        for i in range(0, len(buyable), size):
            batch_no = i // size + 1
            self.ledger.add_batch(run_id, batch_no)
            for line_no in buyable[i:i + size]:
                ln = self.ledger.line(run_id, line_no)
                self.ledger.update_line(run_id, line_no, batch_no=batch_no)
                for _ in range(ln["qty_planned"]):
                    # API mode: the uuid is chosen now and stored BEFORE anything is added.
                    unit_uuid = None if self.shop.assigns_uuids else str(uuidlib.uuid4())
                    self.ledger.add_unit(run_id, batch_no, line_no, ln["prod_id"], unit_uuid, M.U_PLANNED)
        planned = sum(ln["qty_planned"] * ln["price_seen"] for ln in self.ledger.lines(run_id)
                      if ln["batch_no"] is not None)
        n_batches = len(self.ledger.batches(run_id))
        self.log(f"Plan: {len(buyable)} line(s) to buy in {n_batches} batch(es), planned spend ${planned:,.2f}",
                 "bold")

    # =========================================================================
    # Execution
    # =========================================================================
    def execute(self, run_id: str) -> str:
        self.ledger.set_run_status(run_id, M.R_RUNNING, live=self.live or None)
        status = M.R_DRY_RUN_COMPLETE if not self.live else M.R_COMPLETED
        try:
            for b in self.ledger.batches(run_id):
                self.process_batch(run_id, b["batch_no"])
            if self.live and any(b["state"] not in M.DONE_OK for b in self.ledger.batches(run_id)):
                status = M.R_COMPLETED_WITH_ISSUES
        except AbortRun as exc:
            status = M.R_ABORTED
            self.log(f"RUN ABORTED: {exc}", "bold red")
        except KeyboardInterrupt:
            self.ledger.set_run_status(run_id, M.R_INTERRUPTED, "Ctrl+C")
            self.log(f"Interrupted. Continue with:  python -m bot run --resume {run_id}"
                     + (" --live" if self.live else ""), "bold yellow")
            raise
        self.ledger.set_run_status(run_id, status)
        return status

    def process_batch(self, run_id: str, batch_no: int) -> None:
        b = self.ledger.batch(run_id, batch_no)
        state = b["state"]
        if state in M.DONE_OK:
            self.log(f"Batch {batch_no}: already {state} (order {b['order_id'] or 'n/a'}), not repeating", "green")
            return
        if state == M.UNCERTAIN:
            self.log(f"Batch {batch_no}: UNCERTAIN, needs a human to check the account; skipping", "magenta")
            return
        if state == M.FAILED and b["purchase_attempts"] >= MAX_PURCHASE_ATTEMPTS:
            self.log(f"Batch {batch_no}: FAILED after {b['purchase_attempts']} purchase attempts; not retrying", "red")
            return
        if state == M.RECONCILED and not self.live and not any(
                u["state"] == M.U_ADDED for u in self.ledger.units(run_id, batch_no)):
            self.log(f"Batch {batch_no}: already reconciled in an earlier dry run; re-checking", "cyan")

        try:
            if state == M.CHECKOUT_SENT:
                # We crashed or were killed after clicking Purchase. Never click again blindly.
                self.log(f"Batch {batch_no}: found CHECKOUT_SENT from a previous attempt, checking /viewcart "
                         "before doing anything", "yellow")
                if self._resolve_after_click(run_id, batch_no, "resumed after CHECKOUT_SENT"):
                    return
            self._build_cart(run_id, batch_no)
            expected_total = self._reconcile(run_id, batch_no)
            if not self.live:
                self._dry_run_stop(run_id, batch_no, expected_total)
                return
            self._checkout(run_id, batch_no, expected_total)
        except (AbortRun, KeyboardInterrupt):
            raise
        except Exception as exc:  # anything unexpected before Purchase: fail safe and stop the run
            b = self.ledger.batch(run_id, batch_no)
            if b["state"] == M.CHECKOUT_SENT:
                raise
            self._fail(run_id, batch_no, f"{type(exc).__name__}: {exc}")

    # --- step 1: idempotent cart build ------------------------------------------
    def _build_cart(self, run_id: str, batch_no: int) -> None:
        units = self.ledger.units(run_id, batch_no)
        self.log(f"Batch {batch_no}: building cart for {len(units)} unit(s), reading /viewcart first")
        present = {i.uuid: i for i in self.shop.cart_items()}
        missing = [u for u in units if not (u["uuid"] and u["uuid"] in present)]
        missing_ids = {u["unit_id"] for u in missing}
        for u in units:
            if u["unit_id"] not in missing_ids and u["state"] != M.U_ADDED:
                self.ledger.set_unit(u["unit_id"], M.U_ADDED)

        if self.shop.assigns_uuids and missing:
            # UI mode: uuids are generated by the page. Adopt unrecorded cart lines of the
            # right product (e.g. added just before a crash) instead of adding twice.
            known = self.ledger.known_uuids()
            unclaimed = [i for i in present.values() if i.uuid not in known]
            for u in list(missing):
                match = next((i for i in unclaimed if i.prod_id == u["prod_id"]), None)
                if match:
                    self.ledger.set_unit(u["unit_id"], M.U_ADDED, match.uuid)
                    unclaimed.remove(match)
                    missing.remove(u)
                    self.log(f"  adopted unrecorded cart line {match.uuid[:8]} for product {u['prod_id']}")

        already = len(units) - len(missing)
        self.log(f"Batch {batch_no}: {already} unit(s) already in cart, adding {len(missing)} missing",
                 "cyan" if already else None)
        fresh_build = already == 0
        interrupt_after = max(1, len(missing) // 2) if (batch_no in self.sim.interrupt and fresh_build) else None
        for n, u in enumerate(missing, start=1):
            got = self.shop.add_unit(u["uuid"], u["prod_id"])
            self.ledger.set_unit(u["unit_id"], M.U_ADDED, got)
            self.log(f"  + unit {n}/{len(missing)} product {u['prod_id']} uuid {str(got)[:8]}")
            if interrupt_after and n == interrupt_after and n < len(missing):
                self.log(f"SIMULATED interrupt after {n} unit(s) of batch {batch_no}", "magenta")
                raise KeyboardInterrupt
        self.ledger.set_batch_state(run_id, batch_no, M.IN_CART, f"{already} present, {len(missing)} added")

    # --- step 2: pre-checkout reconciliation ------------------------------------
    def _reconcile(self, run_id: str, batch_no: int) -> int:
        units = self.ledger.units(run_id, batch_no)
        prices = {ln["line_no"]: ln["price_seen"] for ln in self.ledger.lines(run_id, batch_no)}
        expected = {u["uuid"]: u for u in units}
        items = self.shop.cart_items()
        present = {i.uuid: i for i in items}

        missing = [uid for uid in expected if uid not in present]
        wrong = [uid for uid in expected if uid in present and present[uid].prod_id is not None
                 and present[uid].prod_id != expected[uid]["prod_id"]]
        strays = [i for i in items if i.uuid not in expected]
        self.log(f"Batch {batch_no}: reconcile /viewcart: expected {len(expected)}, present "
                 f"{len(expected) - len(missing)}, missing {len(missing)}, unexpected {len(strays)}")
        if missing or wrong:
            self._fail(run_id, batch_no, f"reconciliation: {len(missing)} expected unit(s) missing, "
                                         f"{len(wrong)} with the wrong product")
        if strays:
            if self.strict:
                self._fail(run_id, batch_no, f"reconciliation (--strict): {len(strays)} unexpected item(s) in cart")
            for s in strays:
                self.log(f"  removing stray cart line {s.uuid[:8]} (product {s.prod_id})", "yellow")
                self.shop.remove_item(s.uuid)
            again = {i.uuid for i in self.shop.cart_items()}
            if again != set(expected):
                self._fail(run_id, batch_no, "reconciliation: cart still differs after removing strays")

        expected_total = sum(int(prices[u["line_no"]]) for u in units)  # cart.js sums parseInt(price)
        page = self.checkout.read_cart_page()
        page_uuids = {r.uuid for r in page.rows}
        self.log(f"Batch {batch_no}: cart page shows {len(page.rows)} row(s), #totalp = {page.total}; "
                 f"computed total = {expected_total}")
        problems = []
        if page_uuids != set(expected):
            problems.append(f"cart page rows ({len(page.rows)}) do not match the expected {len(expected)} unit(s)")
        if page.total != expected_total:
            problems.append(f"computed total {expected_total} != #totalp {page.total}")
        if problems:
            self._fail(run_id, batch_no, "reconciliation: " + "; ".join(problems))
        self.ledger.set_batch_state(run_id, batch_no, M.RECONCILED, f"total {expected_total} matches #totalp",
                                    expected_total=expected_total, page_total=page.total)
        self.log(f"Batch {batch_no}: RECONCILED: {len(expected)} unit(s), total ${expected_total}", "green")
        return expected_total

    def _dry_run_stop(self, run_id: str, batch_no: int, expected_total: int) -> None:
        shot = self.checkout.screenshot(f"batch{batch_no}-dryrun-cart")
        self.ledger.update_batch(run_id, batch_no, screenshot=shot)
        self.log(f"Batch {batch_no}: DRY RUN, stopping before Purchase (would spend ${expected_total}). "
                 "Use --live to place the order.", "bold yellow")
        # Empty the cart again so the next batch (or a later --live resume) starts clean.
        for u in self.ledger.units(run_id, batch_no):
            self.shop.remove_item(u["uuid"])
            self.ledger.set_unit(u["unit_id"], M.U_REMOVED)
        self.log(f"Batch {batch_no}: dry run, removed its {len(self.ledger.units(run_id, batch_no))} unit(s) "
                 "from the cart again")

    # --- step 3: checkout ---------------------------------------------------------
    def _checkout(self, run_id: str, batch_no: int, expected_total: int) -> None:
        while True:
            b = self.ledger.batch(run_id, batch_no)
            attempt = b["purchase_attempts"] + 1
            sent = False

            def mark_sent() -> None:
                nonlocal sent
                self.ledger.set_batch_state(run_id, batch_no, M.CHECKOUT_SENT, f"Purchase click #{attempt}",
                                            purchase_attempts=attempt)
                sent = True
                self.log(f"Batch {batch_no}: CHECKOUT_SENT (attempt {attempt}), clicking Purchase", "bold")

            self.log(f"Batch {batch_no}: filling the order form (card {self.details.card[:4]} **** **** "
                     f"{self.details.card[-4:]})")
            try:
                text, shot = self.checkout.purchase(
                    self.details, mark_sent,
                    simulate_timeout=batch_no in self.sim.checkout_timeout and attempt == 1)
            except (CheckoutError, Exception) as exc:
                if not sent:
                    self._fail(run_id, batch_no, f"checkout failed before Purchase was clicked: {exc}")
                shot = self.checkout.screenshot(f"batch{batch_no}-checkout-failure")
                self.ledger.update_batch(run_id, batch_no, error=str(exc)[:500], screenshot=shot)
                self.log(f"Batch {batch_no}: checkout failed AFTER Purchase was clicked: {exc}", "bold red")
                self.log("  NOT clicking Purchase again. Checking /viewcart first.", "bold yellow")
                if self._resolve_after_click(run_id, batch_no, str(exc)):
                    return
                # Items are all still there: nothing was bought, safe to retry once.
                self.log(f"Batch {batch_no}: cart unchanged, so nothing was bought, retrying once", "yellow")
                self._build_cart(run_id, batch_no)
                expected_total = self._reconcile(run_id, batch_no)
                continue

            conf = parse_confirmation(text)
            self.log(f"Batch {batch_no}: confirmation dialog: order {conf.order_id}, amount {conf.amount} USD, "
                     f"card ****{conf.card_last4}, name {conf.name}, date {conf.date_raw}", "bold green")
            note = "dialog parsed"
            if conf.amount != expected_total:
                note = f"WARNING dialog amount {conf.amount} != expected {expected_total}"
                self.log(f"  {note}", "bold red")
            if self._cart_cleared(run_id, batch_no) is not True:
                self.log("  site did not empty the cart; clearing it ourselves so the next batch starts clean",
                         "yellow")
                self.shop.clear_cart()
            self._mark_units(run_id, batch_no, M.U_PURCHASED)
            self.ledger.set_batch_state(
                run_id, batch_no, M.CONFIRMED, note, order_id=conf.order_id, amount=conf.amount,
                card_last4=conf.card_last4, name=conf.name, order_date=conf.date_iso,
                order_date_raw=conf.date_raw, confirmation_text=conf.text_masked, screenshot=shot, error=None)
            self.log(f"Batch {batch_no}: CONFIRMED, order {conf.order_id}", "bold green")
            return

    def _cart_cleared(self, run_id: str, batch_no: int) -> bool | None:
        """Poll the cart for a few seconds. True = none of the batch's units remain,
        False = all of them are still there, None = partial or unreadable."""
        expected = {u["uuid"] for u in self.ledger.units(run_id, batch_no)}
        result = None
        for i in range(self.settle_polls):
            try:
                present = {it.uuid for it in self.shop.cart_items()} & expected
            except Exception as exc:
                self.log(f"  cart check {i + 1}/{self.settle_polls} failed: {exc}", "red")
                result = None
            else:
                if not present:
                    return True
                result = False if present == expected else None
                self.log(f"  cart check {i + 1}/{self.settle_polls}: {len(present)}/{len(expected)} unit(s) still in cart")
            if i < self.settle_polls - 1:
                self.sleep(self.settle_interval)
        return result

    def _resolve_after_click(self, run_id: str, batch_no: int, reason: str) -> bool:
        """After an unknown Purchase outcome. Returns True when the batch is settled."""
        cleared = self._cart_cleared(run_id, batch_no)
        b = self.ledger.batch(run_id, batch_no)
        if cleared is True:
            self._mark_units(run_id, batch_no, M.U_PURCHASED)
            self.ledger.set_batch_state(run_id, batch_no, M.CONFIRMED_UNVERIFIED,
                                        "cart emptied by the site -> purchase went through; no dialog captured",
                                        error=f"confirmation not captured ({reason[:200]})")
            self.log(f"Batch {batch_no}: cart is empty -> the purchase went through. CONFIRMED_UNVERIFIED "
                     "(no order id captured). Purchase was NOT clicked again.", "bold yellow")
            return True
        if cleared is None:
            self.ledger.set_batch_state(run_id, batch_no, M.UNCERTAIN, "cart partially emptied or unreadable",
                                        error=f"could not determine outcome ({reason[:200]})")
            self.log(f"Batch {batch_no}: UNCERTAIN: cart partially emptied or unreadable. Stopping for a human.",
                     "bold magenta")
            raise AbortRun(f"batch {batch_no} is UNCERTAIN")
        if b["purchase_attempts"] >= MAX_PURCHASE_ATTEMPTS:
            self._fail(run_id, batch_no, f"items still in cart after {b['purchase_attempts']} purchase attempts")
        self.ledger.set_batch_state(run_id, batch_no, M.RECONCILED, "cart unchanged after click -> not bought")
        return False

    # --- helpers --------------------------------------------------------------------
    def _mark_units(self, run_id: str, batch_no: int, state: str) -> None:
        for u in self.ledger.units(run_id, batch_no):
            self.ledger.set_unit(u["unit_id"], state)

    def _fail(self, run_id: str, batch_no: int, msg: str) -> None:
        shot = None
        try:
            shot = self.checkout.screenshot(f"batch{batch_no}-failure")
        except Exception:
            pass
        self.ledger.set_batch_state(run_id, batch_no, M.FAILED, msg[:300], error=msg[:500], screenshot=shot)
        self.log(f"Batch {batch_no}: FAILED: {msg}", "bold red")
        raise AbortRun(f"batch {batch_no}: {msg}")


def summarize(ledger: Ledger, run_id: str) -> dict:
    lines = ledger.lines(run_id)
    batches = ledger.batches(run_id)
    confirmed = [ln for ln in lines if ln["state"] in M.DONE_OK]
    planned = [ln for ln in lines if ln["batch_no"] is not None]
    return {
        "lines": len(lines),
        "ordered_units": sum(ln["qty_planned"] or 0 for ln in confirmed),
        "confirmed_lines": len(confirmed),
        "skipped": sum(ln["state"] == M.SKIPPED for ln in lines),
        "rejected": sum(ln["decision"] == M.REJECTED for ln in lines),
        "failed": sum(ln["state"] in (M.FAILED, M.UNCERTAIN) for ln in lines),
        "spend": sum((ln["qty_planned"] or 0) * math.floor(ln["price_seen"] or 0) for ln in confirmed),
        "planned_spend": sum((ln["qty_planned"] or 0) * math.floor(ln["price_seen"] or 0) for ln in planned),
        "batches": len(batches),
        "batches_confirmed": sum(b["state"] in M.DONE_OK for b in batches),
        "orders": [b["order_id"] for b in batches if b["order_id"]],
    }
