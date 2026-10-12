"""Throttled Demoblaze API client (https://api.demoblaze.com)."""
from __future__ import annotations

import base64
import time
from typing import Any, Callable

import httpx

from .config import API_BASE
from .models import CartItem, Product


class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class Throttle:
    """Keeps at most `rps` requests per second (0 disables)."""

    def __init__(self, rps: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self.clock, self.sleep = clock, sleep
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        now = self.clock()
        if now < self._next:
            self.sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


class DemoblazeAPI:
    def __init__(self, base_url: str = API_BASE, rps: float = 2.0, client: httpx.Client | None = None,
                 retries: int = 3, on_call: Callable[[str, str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.base = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=20.0)
        self.throttle = Throttle(rps, sleep=sleep)
        self.retries = retries
        self.on_call = on_call
        self.sleep = sleep

    # Every Demoblaze call we make is safe to repeat: /addtocart upserts by the
    # client-chosen uuid, /deleteitem and /deletecart are idempotent. So transport
    # errors and 5xx are retried with backoff.
    def _request(self, method: str, path: str, body: dict | None = None) -> Any:
        attempt = 0
        while True:
            self.throttle.wait()
            try:
                if method == "GET":
                    resp = self.client.get(self.base + path)
                else:
                    resp = self.client.post(self.base + path, json=body)
                if resp.status_code >= 500:
                    raise httpx.HTTPStatusError(f"{resp.status_code} from {path}", request=resp.request, response=resp)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt >= self.retries:
                    raise ApiError(f"{method} {path} failed: {exc}") from exc
                attempt += 1
                if self.on_call:
                    self.on_call(path, f"retry {attempt}/{self.retries} after {type(exc).__name__}")
                self.sleep(2 ** (attempt - 1))
                continue
            if resp.status_code >= 400:
                raise ApiError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
            if self.on_call:
                self.on_call(path, str(resp.status_code))
            text = resp.text.strip()
            if not text:
                return None
            try:
                data = resp.json()
            except ValueError:
                return text
            if isinstance(data, dict) and "errorMessage" in data:
                raise ApiError(f"{path}: {data['errorMessage']}")
            return data

    # --- catalog ---------------------------------------------------------------
    def entries(self) -> list[Product]:
        data = self._request("GET", "/entries")
        raw = list(data.get("Items", []))
        seen = {i["id"] for i in raw}
        key = data.get("LastEvaluatedKey")
        for _ in range(50):  # hard stop, the real catalog has 2 pages
            if not key:
                break
            data = self._request("POST", "/pagination", {"id": key["id"]})
            new = [i for i in (data or {}).get("Items", []) if i["id"] not in seen]
            if not new:
                break
            raw += new
            seen |= {i["id"] for i in new}
            key = data.get("LastEvaluatedKey")
        return [Product(int(i["id"]), i["title"].strip(), float(i["price"]), i.get("cat", "")) for i in raw]

    def view(self, prod_id: int) -> Product:
        i = self._request("POST", "/view", {"id": str(prod_id)})
        return Product(int(i["id"]), i["title"].strip(), float(i["price"]), i.get("cat", ""))

    # --- account ---------------------------------------------------------------
    def signup(self, username: str, password: str) -> None:
        self._request("POST", "/signup", {"username": username, "password": b64(password)})

    def login(self, username: str, password: str) -> str:
        try:
            data = self._request("POST", "/login", {"username": username, "password": b64(password)})
        except ApiError as exc:
            raise AuthError(str(exc)) from exc
        if not isinstance(data, str) or not data.startswith("Auth_token: "):
            raise AuthError(f"unexpected /login response: {data!r}")
        return data.removeprefix("Auth_token: ").strip()

    # --- cart ------------------------------------------------------------------
    def addtocart(self, unit_uuid: str, token: str, prod_id: int) -> None:
        self._request("POST", "/addtocart", {"id": unit_uuid, "cookie": token, "prod_id": int(prod_id), "flag": True})

    def viewcart(self, token: str) -> list[CartItem]:
        data = self._request("POST", "/viewcart", {"cookie": token, "flag": True}) or {}
        return [CartItem(i["id"], int(i["prod_id"])) for i in data.get("Items", [])]

    def deleteitem(self, unit_uuid: str) -> None:
        self._request("POST", "/deleteitem", {"id": unit_uuid})

    def deletecart(self, username: str) -> None:
        self._request("POST", "/deletecart", {"cookie": username})
