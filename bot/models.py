"""Shared data types and the line/batch state machine."""
from __future__ import annotations

from dataclasses import dataclass, field

# --- line / batch states -----------------------------------------------------
PENDING = "PENDING"
VALIDATED = "VALIDATED"
IN_CART = "IN_CART"
RECONCILED = "RECONCILED"
CHECKOUT_SENT = "CHECKOUT_SENT"
CONFIRMED = "CONFIRMED"
CONFIRMED_UNVERIFIED = "CONFIRMED_UNVERIFIED"
SKIPPED = "SKIPPED"
FAILED = "FAILED"
UNCERTAIN = "UNCERTAIN"

DONE_OK = {CONFIRMED, CONFIRMED_UNVERIFIED}

LINE_TRANSITIONS: dict[str, set[str]] = {
    PENDING: {VALIDATED, SKIPPED, FAILED},
    VALIDATED: {IN_CART, SKIPPED, FAILED},
    IN_CART: {RECONCILED, FAILED},
    RECONCILED: {IN_CART, CHECKOUT_SENT, FAILED},
    # CHECKOUT_SENT -> IN_CART/RECONCILED only after /viewcart proved nothing was bought
    CHECKOUT_SENT: {CONFIRMED, CONFIRMED_UNVERIFIED, FAILED, UNCERTAIN, IN_CART, RECONCILED},
    FAILED: {IN_CART},  # resume may rebuild a batch that failed before payment
    CONFIRMED: set(),
    CONFIRMED_UNVERIFIED: set(),
    SKIPPED: set(),
    UNCERTAIN: set(),
}
BATCH_TRANSITIONS = {**LINE_TRANSITIONS, PENDING: {IN_CART, FAILED}}

# --- line decisions ----------------------------------------------------------
BUY = "BUY"
PARTIAL = "PARTIAL"
SKIP_STOCK = "SKIP_STOCK"
SKIP_PRICE = "SKIP_PRICE"
REJECTED = "REJECTED"

# --- cart unit states --------------------------------------------------------
U_PLANNED = "PLANNED"
U_ADDED = "ADDED"
U_REMOVED = "REMOVED"
U_PURCHASED = "PURCHASED"

# --- run statuses ------------------------------------------------------------
R_PLANNED = "PLANNED"
R_RUNNING = "RUNNING"
R_DRY_RUN_COMPLETE = "DRY_RUN_COMPLETE"
R_COMPLETED = "COMPLETED"
R_COMPLETED_WITH_ISSUES = "COMPLETED_WITH_ISSUES"
R_ABORTED = "ABORTED"
R_INTERRUPTED = "INTERRUPTED"


class IllegalTransition(Exception):
    pass


@dataclass(frozen=True)
class Product:
    id: int
    title: str
    price: float
    cat: str = ""


@dataclass(frozen=True)
class CartItem:
    uuid: str
    prod_id: int | None
    title: str = ""
    price: float | None = None


@dataclass
class CartPage:
    """What the cart.html page shows: rows from #tbodyid and the #totalp total."""
    rows: list[CartItem] = field(default_factory=list)
    total: int | None = None
    user_label: str = ""


@dataclass
class Confirmation:
    order_id: str | None
    amount: int | None
    card_last4: str
    name: str
    date_raw: str
    date_iso: str | None
    text_masked: str
