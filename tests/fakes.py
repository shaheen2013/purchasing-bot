"""An in-memory Demoblaze served through httpx.MockTransport, plus a fake browser checkout."""
from __future__ import annotations

import base64
import json
from collections import Counter

import httpx

from bot.api import DemoblazeAPI
from bot.browser import ApiShop, CheckoutError, CheckoutTimeout
from bot.models import CartItem, CartPage

PRODUCTS = [
    {"id": 1, "title": "Samsung galaxy s6", "price": 360.0, "cat": "phone"},
    {"id": 2, "title": "Nokia lumia 1520", "price": 820.0, "cat": "phone"},
    {"id": 3, "title": "Nexus 6", "price": 650.0, "cat": "phone"},
    {"id": 10, "title": "Apple monitor 24", "price": 400.0, "cat": "monitor"},
    {"id": 11, "title": "MacBook air", "price": 700.0, "cat": "notebook"},
]
USER, PASSWORD, TOKEN = "alice", "secret", "dG9rLWFsaWNl"


class FakeDemoblaze:
    def __init__(self):
        self.products = {p["id"]: p for p in PRODUCTS}
        self.cart: dict[str, int] = {}          # uuid -> prod_id (one user)
        self.add_calls: Counter[str] = Counter()  # uuid -> number of /addtocart calls
        self.fail_add_on_call: int | None = None  # simulate a network failure on the Nth add
        self.drop_add_on_call: int | None = None  # silently lose the Nth add
        self.calls: list[str] = []

    @property
    def total_adds(self) -> int:
        return sum(self.add_calls.values())

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        if path == "/entries":
            return httpx.Response(200, json={"Items": list(self.products.values()), "LastEvaluatedKey": {"id": "11"}})
        if path == "/pagination":
            return httpx.Response(200, json={"Items": []})
        if path == "/view":
            p = self.products.get(int(body["id"]))
            return httpx.Response(200, json=p or {"errorMessage": "Not found."})
        if path == "/login":
            ok = body["username"] == USER and body["password"] == base64.b64encode(PASSWORD.encode()).decode()
            return httpx.Response(200, json=f"Auth_token: {TOKEN}" if ok else {"errorMessage": "Wrong password."})
        if path == "/addtocart":
            assert body["cookie"] == TOKEN and body["flag"] is True
            n = self.total_adds + 1
            if self.fail_add_on_call == n:
                self.fail_add_on_call = None
                raise httpx.ConnectError("simulated network failure", request=request)
            self.add_calls[body["id"]] += 1
            if self.drop_add_on_call == n:
                return httpx.Response(200, text="")
            self.cart[body["id"]] = int(body["prod_id"])
            return httpx.Response(200, text="")
        if path == "/viewcart":
            if body["cookie"] != TOKEN:
                return httpx.Response(200, json={"errorMessage": "Bad parameter, token malformed."})
            return httpx.Response(200, json={"Items": [{"id": k, "prod_id": v, "cookie": USER}
                                                       for k, v in self.cart.items()]})
        if path == "/deleteitem":
            self.cart.pop(body["id"], None)
            return httpx.Response(200, json="Item deleted.")
        if path == "/deletecart":
            if body["cookie"] == USER:
                self.cart.clear()
            return httpx.Response(200, json="Item deleted.")
        return httpx.Response(404, text="not found")

    def api(self) -> DemoblazeAPI:
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return DemoblazeAPI("https://api.test", rps=0, client=client, retries=0, sleep=lambda s: None)

    def shop(self) -> ApiShop:
        api = self.api()
        return ApiShop(api, api.login(USER, PASSWORD), USER)


class FakeCheckout:
    """Stands in for the Playwright cart page / order modal."""

    def __init__(self, server: FakeDemoblaze, *, page_total_delta: int = 0, timeout_after_click: int = 0,
                 clear_cart_on_click: bool = True, fail_before_click: bool = False):
        self.server = server
        self.page_total_delta = page_total_delta
        self.timeouts_left = timeout_after_click   # how many clicks end in a timeout
        self.clear_cart_on_click = clear_cart_on_click
        self.fail_before_click = fail_before_click
        self.purchase_clicks = 0
        self.screenshots: list[str] = []

    def read_cart_page(self) -> CartPage:
        rows = [CartItem(u, pid, self.server.products[pid]["title"], self.server.products[pid]["price"])
                for u, pid in self.server.cart.items()]
        total = sum(int(r.price) for r in rows) + self.page_total_delta if rows else None
        return CartPage(rows=rows, total=total, user_label=f"Welcome {USER}")

    def purchase(self, details, before_click, simulate_timeout=False):
        if self.fail_before_click:
            raise CheckoutError("order modal did not open")
        total = sum(int(self.server.products[p]["price"]) for p in self.server.cart.values())
        before_click()
        self.purchase_clicks += 1
        if self.clear_cart_on_click:
            self.server.cart.clear()  # what purchaseOrder() does via /deletecart
        if self.timeouts_left > 0 or simulate_timeout:
            self.timeouts_left -= 1
            raise CheckoutTimeout("confirmation dialog not seen")
        text = (f"Id: {1000 + self.purchase_clicks}\nAmount: {total} USD\nCard Number: {details.card}\n"
                f"Name: {details.name}\nDate: 12/9/2026")
        return text, None

    def screenshot(self, name):
        self.screenshots.append(name)
        return None
