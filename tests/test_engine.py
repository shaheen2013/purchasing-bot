from pathlib import Path

import pytest

from bot import models as M
from bot.config import CheckoutDetails
from bot.engine import Engine
from bot.ledger import Ledger
from bot.simulate import parse_simulations

from .fakes import FakeCheckout, FakeDemoblaze

DETAILS = CheckoutDetails("Demo Buyer", "Testland", "Sample City", "4111111111111111", "12", "2030")


@pytest.fixture
def server():
    return FakeDemoblaze()


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.db")
    yield led
    led.close()


@pytest.fixture
def orders(tmp_path) -> Path:
    p = tmp_path / "orders.csv"
    p.write_text("product,qty,max_unit_price\n"
                 "1,3,400\n"                 # 3 x 360
                 "nexus 6,2,700\n"           # 2 x 650 (title match, case-insensitive)
                 "Apple monitor 24,1,450\n"  # 1 x 400
                 "Unknown Phone,1,100\n"     # rejected: unknown product
                 "MacBook air,0,800\n",      # rejected: qty <= 0
                 encoding="utf-8")
    return p


def make_engine(ledger, server, checkout, *, live, sim=None, strict=False):
    return Engine(ledger, server.shop(), checkout, DETAILS, live=live, strict=strict, sim=sim,
                  settle_polls=3, settle_interval=0, sleep=lambda s: None)


def plan(engine, run_id, orders, batch_size=0, **kw):
    engine.plan(run_id, orders, mode="api", batch_size=batch_size, **kw)


# --- planning & guards -------------------------------------------------------------
def test_plan_rejects_bad_rows_and_generates_uuids_up_front(ledger, server, orders):
    eng = make_engine(ledger, server, FakeCheckout(server), live=False)
    plan(eng, "r1", orders)
    lines = {ln["line_no"]: ln for ln in ledger.lines("r1")}
    assert lines[4]["decision"] == M.REJECTED and "unknown product" in lines[4]["error"]
    assert lines[5]["decision"] == M.REJECTED and "qty must be > 0" in lines[5]["error"]
    assert lines[2]["prod_id"] == 3
    units = ledger.units("r1")
    assert len(units) == 6 and all(u["uuid"] for u in units)  # stored before any add
    assert server.total_adds == 0


def test_price_drift_and_stock_guards(ledger, server, orders, tmp_path):
    stock = tmp_path / "stock.yaml"
    stock.write_text("10: 5\n3: 1\n", encoding="utf-8")
    sim = parse_simulations(["price_drift=1:20", "stock_out=10"])
    eng = make_engine(ledger, server, FakeCheckout(server), live=False, sim=sim)
    plan(eng, "r1", orders, stock_path=stock)
    lines = {ln["line_no"]: ln for ln in ledger.lines("r1")}
    assert lines[1]["decision"] == M.SKIP_PRICE and lines[1]["price_seen"] == 432.0   # 360 * 1.2 > 400
    assert lines[2]["decision"] == M.SKIP_STOCK                                        # want 2, have 1
    assert lines[3]["decision"] == M.SKIP_STOCK                                        # stock_out


def test_allow_partial_buys_what_is_left(ledger, server, orders, tmp_path):
    stock = tmp_path / "stock.yaml"
    stock.write_text("3: 1\n", encoding="utf-8")
    eng = make_engine(ledger, server, FakeCheckout(server), live=False)
    plan(eng, "r1", orders, stock_path=stock, allow_partial=True)
    ln = ledger.line("r1", 2)
    assert ln["decision"] == M.PARTIAL and ln["qty_planned"] == 1


# --- required: resume never re-adds units already present ---------------------------
def test_resume_only_adds_missing_units(ledger, server, orders):
    server.fail_add_on_call = 4  # network dies on the 4th /addtocart
    eng = make_engine(ledger, server, FakeCheckout(server), live=False)
    plan(eng, "r1", orders)
    assert eng.execute("r1") == M.R_ABORTED
    assert len(server.cart) == 3

    # resume with a fresh engine (as after a restart), this time live
    checkout = FakeCheckout(server)
    eng2 = make_engine(ledger, server, checkout, live=True)
    assert eng2.execute("r1") == M.R_COMPLETED

    planned = {u["uuid"] for u in ledger.units("r1")}
    assert set(server.add_calls) == planned
    assert all(n == 1 for n in server.add_calls.values()), server.add_calls  # nothing added twice
    assert checkout.purchase_clicks == 1
    b = ledger.batch("r1", 1)
    assert b["state"] == M.CONFIRMED and b["order_id"] == "1001" and b["card_last4"] == "1111"
    assert b["amount"] == 3 * 360 + 2 * 650 + 400

    # resuming a finished run does not touch the cart or buy again
    adds_before = server.total_adds
    assert eng2.execute("r1") == M.R_COMPLETED
    assert server.total_adds == adds_before and checkout.purchase_clicks == 1


def test_interrupt_mid_batch_then_resume_skips_confirmed_batch(ledger, server, orders):
    checkout = FakeCheckout(server)
    eng = make_engine(ledger, server, checkout, live=True, sim=parse_simulations(["interrupt=2"]))
    plan(eng, "r1", orders, batch_size=1)  # batch 1: 3 units, batch 2: 2 units, batch 3: 1 unit
    with pytest.raises(KeyboardInterrupt):
        eng.execute("r1")
    assert ledger.batch("r1", 1)["state"] == M.CONFIRMED
    assert ledger.get_run("r1")["status"] == M.R_INTERRUPTED
    assert len(server.cart) == 1  # half of batch 2 was added before the stop

    eng2 = make_engine(ledger, server, checkout, live=True)
    assert eng2.execute("r1") == M.R_COMPLETED
    assert checkout.purchase_clicks == 3  # one per batch, batch 1 not repeated
    assert [b["order_id"] for b in ledger.batches("r1")] == ["1001", "1002", "1003"]
    assert all(n == 1 for n in server.add_calls.values())


def test_dry_run_then_resume_live(ledger, server, orders):
    eng = make_engine(ledger, server, FakeCheckout(server), live=False)
    plan(eng, "r1", orders)
    assert eng.execute("r1") == M.R_DRY_RUN_COMPLETE
    assert ledger.batch("r1", 1)["state"] == M.RECONCILED
    assert server.cart == {}  # the dry run leaves the cart empty again
    checkout = FakeCheckout(server)
    assert make_engine(ledger, server, checkout, live=True).execute("r1") == M.R_COMPLETED
    assert checkout.purchase_clicks == 1


# --- required: checkout timeout + empty cart -> CONFIRMED_UNVERIFIED, no 2nd purchase ---
def test_checkout_timeout_with_empty_cart_is_confirmed_unverified(ledger, server, orders):
    checkout = FakeCheckout(server, timeout_after_click=1, clear_cart_on_click=True)
    eng = make_engine(ledger, server, checkout, live=True)
    plan(eng, "r1", orders)
    eng.execute("r1")
    assert checkout.purchase_clicks == 1
    b = ledger.batch("r1", 1)
    assert b["state"] == M.CONFIRMED_UNVERIFIED
    assert all(ln["state"] == M.CONFIRMED_UNVERIFIED for ln in ledger.lines("r1", 1))
    # and a resume does not buy again either
    make_engine(ledger, server, checkout, live=True).execute("r1")
    assert checkout.purchase_clicks == 1


def test_checkout_timeout_with_items_still_in_cart_retries_once(ledger, server, orders):
    checkout = FakeCheckout(server, timeout_after_click=1, clear_cart_on_click=False)
    eng = make_engine(ledger, server, checkout, live=True)
    plan(eng, "r1", orders)
    # first click: timeout and cart untouched -> safe to retry; second click succeeds but
    # the fake never clears the cart, so the bot clears it itself via /deletecart.
    assert eng.execute("r1") == M.R_COMPLETED
    assert checkout.purchase_clicks == 2
    b = ledger.batch("r1", 1)
    assert b["state"] == M.CONFIRMED and b["purchase_attempts"] == 2
    assert server.cart == {}


def test_two_failed_attempts_stop_without_third_click(ledger, server, orders):
    checkout = FakeCheckout(server, timeout_after_click=5, clear_cart_on_click=False)
    eng = make_engine(ledger, server, checkout, live=True)
    plan(eng, "r1", orders)
    assert eng.execute("r1") == M.R_ABORTED
    assert checkout.purchase_clicks == 2
    assert ledger.batch("r1", 1)["state"] == M.FAILED
    make_engine(ledger, server, checkout, live=True).execute("r1")
    assert checkout.purchase_clicks == 2


def test_crash_after_click_is_resolved_on_resume(ledger, server, orders):
    eng = make_engine(ledger, server, FakeCheckout(server), live=True)
    plan(eng, "r1", orders)
    eng._build_cart("r1", 1)
    eng._reconcile("r1", 1)
    # simulate: CHECKOUT_SENT written, Purchase clicked, site emptied cart, then the process died
    ledger.set_batch_state("r1", 1, M.CHECKOUT_SENT, purchase_attempts=1)
    server.cart.clear()
    checkout = FakeCheckout(server)
    make_engine(ledger, server, checkout, live=True).execute("r1")
    assert checkout.purchase_clicks == 0
    assert ledger.batch("r1", 1)["state"] == M.CONFIRMED_UNVERIFIED


# --- required: reconciliation mismatch aborts ------------------------------------------
def test_reconciliation_total_mismatch_aborts_before_purchase(ledger, server, orders):
    checkout = FakeCheckout(server, page_total_delta=10)
    eng = make_engine(ledger, server, checkout, live=True)
    plan(eng, "r1", orders)
    assert eng.execute("r1") == M.R_ABORTED
    b = ledger.batch("r1", 1)
    assert b["state"] == M.FAILED and "#totalp" in b["error"]
    assert checkout.purchase_clicks == 0
    assert "batch1-failure" in checkout.screenshots


def test_reconciliation_missing_unit_aborts(ledger, server, orders):
    server.drop_add_on_call = 2  # server accepts the call but loses the item
    checkout = FakeCheckout(server)
    eng = make_engine(ledger, server, checkout, live=True)
    plan(eng, "r1", orders)
    assert eng.execute("r1") == M.R_ABORTED
    assert "missing" in ledger.batch("r1", 1)["error"]
    assert checkout.purchase_clicks == 0


def test_stray_items_are_removed_or_abort_with_strict(ledger, server, orders):
    server.cart["stray-uuid"] = 2
    checkout = FakeCheckout(server)
    eng = make_engine(ledger, server, checkout, live=True, strict=True)
    plan(eng, "r1", orders)
    assert eng.execute("r1") == M.R_ABORTED
    assert "unexpected" in ledger.batch("r1", 1)["error"] and checkout.purchase_clicks == 0

    eng2 = make_engine(ledger, server, checkout, live=True, strict=False)
    assert eng2.execute("r1") == M.R_COMPLETED
    assert "stray-uuid" not in server.add_calls and checkout.purchase_clicks == 1


def test_price_drift_that_passes_the_guard_is_caught_by_reconciliation(ledger, server, orders):
    checkout = FakeCheckout(server)
    eng = make_engine(ledger, server, checkout, live=True, sim=parse_simulations(["price_drift=1:5"]))
    plan(eng, "r1", orders)  # 378 <= 400 passes the guard, but the cart shows 360
    assert eng.execute("r1") == M.R_ABORTED
    assert checkout.purchase_clicks == 0


def test_failure_before_click_is_failed_not_uncertain(ledger, server, orders):
    checkout = FakeCheckout(server, fail_before_click=True)
    eng = make_engine(ledger, server, checkout, live=True)
    plan(eng, "r1", orders)
    assert eng.execute("r1") == M.R_ABORTED
    b = ledger.batch("r1", 1)
    assert b["state"] == M.FAILED and b["purchase_attempts"] == 0
