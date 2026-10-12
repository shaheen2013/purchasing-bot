"""orders.csv parsing, catalog matching, and the price / simulated-stock guards."""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import BUY, PARTIAL, SKIP_STOCK, Product

REQUIRED_COLUMNS = ("product", "qty", "max_unit_price")


def norm_title(text: str) -> str:
    return " ".join(text.split()).casefold()


class Catalog:
    def __init__(self, products: list[Product]):
        self.by_id = {p.id: p for p in products}
        self.by_title = {norm_title(p.title): p for p in products}

    def __len__(self) -> int:
        return len(self.by_id)

    def resolve(self, text: str) -> Product | None:
        text = (text or "").strip()
        if text.isdigit() and int(text) in self.by_id:
            return self.by_id[int(text)]
        return self.by_title.get(norm_title(text))


@dataclass
class OrderRow:
    line_no: int
    product: str
    qty: str
    max_unit_price: str


@dataclass
class ValidatedRow:
    product: Product | None = None
    qty: int | None = None
    max_unit_price: float | None = None
    errors: list[str] = field(default_factory=list)


class OrdersFileError(Exception):
    pass


def read_orders(path: Path) -> list[OrderRow]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = [c.strip().lower() for c in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in cols]
        if missing:
            raise OrdersFileError(f"{path}: missing column(s) {', '.join(missing)}")
        rows = []
        for n, raw in enumerate(reader, start=1):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            rows.append(OrderRow(n, row["product"], row["qty"], row["max_unit_price"]))
        return rows


def validate_row(row: OrderRow, catalog: Catalog) -> ValidatedRow:
    v = ValidatedRow()
    if not row.product:
        v.errors.append("product is empty")
    else:
        v.product = catalog.resolve(row.product)
        if v.product is None:
            v.errors.append(f"unknown product {row.product!r}")
    try:
        v.qty = int(row.qty)
        if v.qty <= 0:
            v.errors.append(f"qty must be > 0 (got {v.qty})")
    except ValueError:
        v.errors.append(f"qty is not a whole number ({row.qty!r})")
    try:
        v.max_unit_price = float(row.max_unit_price)
        if not math.isfinite(v.max_unit_price) or v.max_unit_price <= 0:
            v.errors.append(f"max_unit_price must be > 0 (got {row.max_unit_price!r})")
    except ValueError:
        v.errors.append(f"max_unit_price is not a number ({row.max_unit_price!r})")
    return v


# --- simulated stock ----------------------------------------------------------
def load_stock(path: Path | None) -> dict[int, int] | None:
    if path is None or not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise OrdersFileError(f"{path}: expected a mapping prod_id: available")
    return {int(k): int(v) for k, v in data.items()}


class StockBook:
    """SIMULATED inventory. Demoblaze has none; this only exists to show the guard."""

    def __init__(self, available: dict[int, int] | None, stock_out: set[int] = frozenset()):
        self.available = dict(available or {})
        for pid in stock_out:
            self.available[pid] = 0

    @property
    def active(self) -> bool:
        return bool(self.available)

    def allocate(self, prod_id: int, qty: int, allow_partial: bool) -> tuple[str, int, str]:
        """Return (decision, qty_to_buy, note) and consume the allocated units."""
        if prod_id not in self.available:
            return BUY, qty, ""
        have = self.available[prod_id]
        if have >= qty:
            self.available[prod_id] = have - qty
            return BUY, qty, f"stock {have} -> {have - qty} (simulated)"
        if allow_partial and have > 0:
            self.available[prod_id] = 0
            return PARTIAL, have, f"only {have} of {qty} in stock (simulated)"
        return SKIP_STOCK, 0, f"only {have} of {qty} in stock (simulated)"
