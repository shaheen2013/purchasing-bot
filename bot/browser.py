"""Playwright (sync, Chromium): the cart page, the checkout and the fully UI-driven shop."""
from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from typing import Callable

from .config import CheckoutDetails
from .models import CartItem, CartPage, Confirmation, Product
from .orders import Catalog, norm_title


class CheckoutError(Exception):
    """Checkout could not complete. Whether Purchase was clicked is tracked by the engine."""


class CheckoutTimeout(CheckoutError):
    pass


class ShopError(Exception):
    pass


# --- SweetAlert parsing ---------------------------------------------------------
_LABELS = ("Id", "Amount", "Card Number", "Name", "Date")
_FIELD_RE = {
    label: re.compile(rf"{label}:\s*(.*?)\s*(?=(?:{'|'.join(_LABELS)}):|$)", re.S) for label in _LABELS
}


def mask_card(card: str) -> str:
    digits = re.sub(r"\D", "", card)
    return f"**** **** **** {digits[-4:]}" if digits else ""


def parse_confirmation(text: str) -> Confirmation:
    """Parse the purchaseOrder() SweetAlert text:
    'Id: 123\\nAmount: 790 USD\\nCard Number: 4111...\\nName: X\\nDate: 12/9/2026'."""
    text = (text or "").strip()
    fields = {}
    for label, rx in _FIELD_RE.items():
        m = rx.search(text)
        fields[label] = m.group(1).strip() if m else ""
    amount = re.match(r"-?\d+", fields["Amount"])
    card_digits = re.sub(r"\D", "", fields["Card Number"])
    date_raw = fields["Date"]
    date_iso = None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_raw)
    if m:
        # cart.js builds the date with JavaScript's 0-based getMonth(), so "12/9/2026" is 12 Oct 2026.
        try:
            date_iso = date(int(m.group(3)), int(m.group(2)) + 1, int(m.group(1))).isoformat()
        except ValueError:
            date_iso = None
    masked = text.replace(fields["Card Number"], mask_card(card_digits)) if fields["Card Number"] else text
    return Confirmation(
        order_id=fields["Id"] or None,
        amount=int(amount.group(0)) if amount else None,
        card_last4=card_digits[-4:],
        name=fields["Name"],
        date_raw=date_raw,
        date_iso=date_iso,
        text_masked=masked,
    )


def _price(text: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", text or "")
    return float(m.group(0)) if m else None


# --- browser session -------------------------------------------------------------
class BrowserSession:
    """Lazily started Chromium. Native alerts are recorded and accepted."""

    def __init__(self, web_base: str, screens_dir: Path, headed: bool = False, slowmo: int = 0,
                 log: Callable[[str], None] | None = None):
        self.web_base = web_base
        self.screens_dir = Path(screens_dir)
        self.headed, self.slowmo = headed, slowmo
        self.log = log or (lambda msg: None)
        self.dialogs: list[str] = []
        self._token: str | None = None
        self._pw = self._browser = self._context = self._page = None

    @property
    def started(self) -> bool:
        return self._page is not None

    def set_token(self, token: str) -> None:
        """API mode: reuse the API login by handing the browser the same token cookie."""
        self._token = token
        if self._context is not None:
            self._add_cookie()

    def _add_cookie(self) -> None:
        self._context.add_cookies([{"name": "tokenp_", "value": self._token, "url": self.web_base}])

    @property
    def page(self):
        if self._page is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=not self.headed, slow_mo=self.slowmo)
            self._context = self._browser.new_context(viewport={"width": 1280, "height": 900})
            self._context.set_default_timeout(20_000)
            if self._token:
                self._add_cookie()
            self._page = self._context.new_page()
            self._page.on("dialog", self._on_dialog)
        return self._page

    def _on_dialog(self, dialog) -> None:
        self.dialogs.append(dialog.message)
        self.log(f"[browser] native alert {dialog.message!r} -> accepted")
        try:
            dialog.accept()
        except Exception:
            pass

    def wait_for_dialog(self, start_index: int, needle: str, timeout_s: float = 10.0) -> str | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for msg in self.dialogs[start_index:]:
                if needle.lower() in msg.lower():
                    return msg
            self.page.wait_for_timeout(150)
        return None

    def screenshot(self, name: str) -> str | None:
        if self._page is None:
            return None
        self.screens_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)
        path = self.screens_dir / f"{time.strftime('%H%M%S')}-{safe}.png"
        try:
            self._page.screenshot(path=str(path), full_page=True)
        except Exception as exc:
            self.log(f"[browser] screenshot failed: {exc}")
            return None
        return str(path)

    def close(self) -> None:
        for closer in (lambda: self._context and self._context.close(),
                       lambda: self._browser and self._browser.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._pw = self._browser = self._context = self._page = None


_ROWS_JS = """rows => rows.map(r => {
  const tds = r.querySelectorAll('td');
  const a = r.querySelector('a[onclick]');
  const m = a ? /deleteItem\\('([^']+)'\\)/.exec(a.getAttribute('onclick')) : null;
  return {title: tds[1] ? tds[1].textContent.trim() : '', price: tds[2] ? tds[2].textContent.trim() : '',
          uuid: m ? m[1] : null};
})"""


class PageCheckout:
    """Reads cart.html and performs the only step Demoblaze has no API for: Place Order."""

    def __init__(self, session: BrowserSession, confirm_timeout_ms: int = 15_000):
        self.session = session
        self.confirm_timeout_ms = confirm_timeout_ms

    def read_cart_page(self) -> CartPage:
        from playwright.sync_api import TimeoutError as PWTimeout

        page = self.session.page
        is_viewcart = lambda r: r.url.endswith("/viewcart") and r.request.method == "POST"  # noqa: E731
        try:
            with page.expect_response(is_viewcart, timeout=30_000) as info:
                page.goto(f"{self.session.web_base}/cart.html", wait_until="domcontentloaded")
            expected_rows = len((info.value.json() or {}).get("Items", []))
            # cart.js appends one row per item after a /view call each; wait for all of them.
            page.wait_for_function("n => document.querySelectorAll('#tbodyid > tr').length >= n",
                                   arg=expected_rows, timeout=30_000)
        except PWTimeout as exc:
            raise CheckoutError(f"cart page did not finish loading: {exc}") from exc
        raw_rows = page.eval_on_selector_all("#tbodyid > tr", _ROWS_JS)
        total_text = (page.text_content("#totalp") or "").strip()
        user = ""
        try:
            # usern is set by /check; purchaseOrder() needs it to clear the right cart.
            page.wait_for_selector("#nameofuser", state="visible", timeout=10_000)
            user = (page.text_content("#nameofuser") or "").strip()
        except PWTimeout:
            pass
        rows = [CartItem(r["uuid"] or "", None, r["title"], _price(r["price"])) for r in raw_rows]
        return CartPage(rows=rows, total=int(total_text) if total_text.isdigit() else None, user_label=user)

    def purchase(self, details: CheckoutDetails, before_click: Callable[[], None],
                 simulate_timeout: bool = False) -> tuple[str, str | None]:
        """Fill #orderModal and click Purchase once. Returns (dialog text, screenshot path).

        `before_click` runs immediately before the click so the ledger records
        CHECKOUT_SENT first; any error after that point is a post-click failure.
        """
        from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout

        page = self.session.page
        if not page.url.endswith("/cart.html"):
            raise CheckoutError("browser is not on cart.html")
        if not page.is_visible("#nameofuser"):
            raise CheckoutError("browser is not logged in; Purchase would not clear this account's cart")
        try:
            page.click("button[data-target='#orderModal']")
            page.wait_for_selector("#orderModal", state="visible")
            page.wait_for_timeout(500)  # bootstrap fade-in
            for selector, value in (("#name", details.name), ("#country", details.country),
                                    ("#city", details.city), ("#card", details.card),
                                    ("#month", details.month), ("#year", details.year)):
                page.fill(selector, value)
        except PWError as exc:
            raise CheckoutError(f"could not fill the order form: {exc}") from exc

        # purchaseOrder() fires /deletecart and shows the dialog without waiting for it; clicking
        # OK navigates away and can abort that request. So watch for its response first.
        deletecart_status: list[int] = []
        on_response = lambda r: deletecart_status.append(r.status) if r.url.endswith("/deletecart") else None  # noqa: E731
        page.on("response", on_response)
        before_click()
        try:
            page.click("#orderModal button[onclick='purchaseOrder()']", timeout=10_000)
            if simulate_timeout:
                page.wait_for_timeout(3_000)
                raise CheckoutTimeout("SIMULATED: no confirmation dialog seen after Purchase")
            page.wait_for_selector(".sweet-alert h2:has-text('Thank you for your purchase!')",
                                   state="visible", timeout=self.confirm_timeout_ms)
            text = page.text_content(".sweet-alert p") or ""
        except PWTimeout as exc:
            raise CheckoutTimeout(f"confirmation dialog not seen: {exc}") from exc
        except PWError as exc:
            raise CheckoutTimeout(f"browser error after Purchase: {exc}") from exc
        shot = self.session.screenshot("confirmation")
        deadline = time.monotonic() + 10
        while not deletecart_status and time.monotonic() < deadline:
            page.wait_for_timeout(200)
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        if not deletecart_status:
            self.session.log("[browser] site's /deletecart response not seen within 10s")
        try:
            page.click(".sweet-alert button.confirm", timeout=5_000)
        except PWError:
            pass
        return text, shot

    def screenshot(self, name: str) -> str | None:
        if not self.session.started:
            try:
                self.read_cart_page()  # so a failure screenshot always shows the cart state
            except Exception:
                pass
        return self.session.screenshot(name)


# --- shops -----------------------------------------------------------------------
class ApiShop:
    """API-first: catalog, prices and the cart through api.demoblaze.com."""

    assigns_uuids = False

    def __init__(self, api, token: str, username: str):
        self.api, self.token, self.username = api, token, username

    def catalog(self) -> list[Product]:
        return self.api.entries()

    def live_price(self, prod_id: int) -> float:
        return self.api.view(prod_id).price

    def cart_items(self) -> list[CartItem]:
        return self.api.viewcart(self.token)

    def add_unit(self, unit_uuid: str, prod_id: int) -> str:
        self.api.addtocart(unit_uuid, self.token, prod_id)
        return unit_uuid

    def remove_item(self, unit_uuid: str) -> None:
        self.api.deleteitem(unit_uuid)

    def clear_cart(self) -> None:
        self.api.deletecart(self.username)


class BrowserShop:
    """Fallback: the same operations driven through the website UI only."""

    assigns_uuids = True  # the page generates its own uuid; we capture it from the request

    def __init__(self, session: BrowserSession, checkout: PageCheckout, username: str, password: str):
        self.session, self.checkout = session, checkout
        self.username, self.password = username, password
        self._catalog: Catalog | None = None

    @property
    def web(self) -> str:
        return self.session.web_base

    def login(self) -> None:
        from playwright.sync_api import TimeoutError as PWTimeout

        page = self.session.page
        page.goto(f"{self.web}/index.html")
        page.click("#login2")
        page.wait_for_selector("#logInModal", state="visible")
        page.wait_for_timeout(400)
        page.fill("#loginusername", self.username)
        page.fill("#loginpassword", self.password)
        seen = len(self.session.dialogs)
        page.click("#logInModal button[onclick='logIn()']")
        try:
            page.wait_for_function(
                "u => { const e = document.getElementById('nameofuser');"
                " return e && e.offsetParent !== null && e.textContent.includes(u); }",
                arg=self.username, timeout=20_000)
        except PWTimeout as exc:
            alerts = "; ".join(self.session.dialogs[seen:]) or "timed out"
            raise ShopError(f"UI login failed: {alerts}") from exc

    def catalog(self) -> list[Product]:
        page = self.session.page
        is_entries = lambda r: r.url.endswith("/entries")  # noqa: E731
        with page.expect_response(is_entries):
            page.goto(f"{self.web}/index.html")
        page.wait_for_selector("#tbodyid .card")
        products: dict[int, Product] = {}
        for _ in range(20):
            cards = page.eval_on_selector_all(
                "#tbodyid .card",
                """cs => cs.map(c => { const a = c.querySelector('.card-title a');
                   const h = c.querySelector('h5');
                   return {href: a ? a.getAttribute('href') : '', title: a ? a.textContent : '',
                           price: h ? h.textContent : ''}; })""")
            new = 0
            for c in cards:
                m = re.search(r"idp_=(\d+)", c["href"] or "")
                if m and int(m.group(1)) not in products:
                    pid = int(m.group(1))
                    products[pid] = Product(pid, c["title"].strip(), _price(c["price"]) or 0.0)
                    new += 1
            if not new or not page.is_visible("#next2"):
                break
            first = cards[0]["href"] if cards else ""
            with page.expect_response(lambda r: r.url.endswith("/pagination")) as info:
                page.click("#next2")
            if not (info.value.json() or {}).get("Items"):
                break
            try:
                page.wait_for_function(
                    "f => { const a = document.querySelector('#tbodyid .card .card-title a');"
                    " return a && a.getAttribute('href') !== f; }", arg=first, timeout=10_000)
            except Exception:
                break
        self._catalog = Catalog(list(products.values()))
        return list(products.values())

    def _open_product(self, prod_id: int):
        page = self.session.page
        page.goto(f"{self.web}/prod.html?idp_={prod_id}")
        page.wait_for_function("() => { const e = document.querySelector('.name');"
                               " return e && e.textContent.trim().length > 0; }")
        return page

    def live_price(self, prod_id: int) -> float:
        page = self._open_product(prod_id)
        price = _price(page.text_content(".price-container") or "")
        if price is None:
            raise ShopError(f"no price shown on prod.html?idp_={prod_id}")
        return price

    def cart_items(self) -> list[CartItem]:
        if self._catalog is None:
            self.catalog()
        cart = self.checkout.read_cart_page()
        out = []
        for row in cart.rows:
            prod = self._catalog.by_title.get(norm_title(row.title))
            out.append(CartItem(row.uuid, prod.id if prod else None, row.title, row.price))
        return out

    def add_unit(self, unit_uuid: str | None, prod_id: int) -> str:
        page = self._open_product(prod_id)
        seen = len(self.session.dialogs)
        is_add = lambda r: r.url.endswith("/addtocart") and r.method == "POST"  # noqa: E731
        with page.expect_request(is_add) as info:
            page.click("a.btn-success:has-text('Add to cart')")
        captured = (info.value.post_data_json or {}).get("id")
        if not self.session.wait_for_dialog(seen, "Product added"):
            raise ShopError(f"no 'Product added' alert for product {prod_id}")
        return captured

    def remove_item(self, unit_uuid: str) -> None:
        page = self.session.page
        if not page.url.endswith("/cart.html"):
            self.checkout.read_cart_page()
        with page.expect_response(lambda r: r.url.endswith("/deleteitem")):
            page.click(f"a[onclick=\"deleteItem('{unit_uuid}')\"]")
        page.wait_for_load_state()

    def clear_cart(self) -> None:
        for item in self.cart_items():
            self.remove_item(item.uuid)
