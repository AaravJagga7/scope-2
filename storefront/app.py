"""Bazaaro storefront + SCOPE-X Constraint Lab (FastAPI).

Run with ``python -m storefront`` and open http://127.0.0.1:8010.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from storefront.catalog import CATEGORIES, PRODUCTS_BY_ID, search
from storefront.drop import run_drop
from storefront.lab import (
    SLO,
    TESTBED_LABEL,
    LoadRunner,
    LoadTest,
    StoreTelemetry,
    WhatIfConfig,
    load_constraint_registry,
    what_if,
)
from storefront.shield import Shield
from storefront.testbed import URLS, Testbed, TestbedConfig

STATIC_DIR = Path(__file__).resolve().parent / "static"
CART_COOKIE = "bz_cart"

testbed = Testbed()
runner = LoadRunner()
telemetry = StoreTelemetry()
# The store runs with all three shield layers on unless SHIELD_ENABLED=0.
shield = Shield()
if os.getenv("SHIELD_ENABLED", "1") == "1":
    shield.configure({"waiting_room": True, "reservations": True, "bot_defense": True})
slo = SLO(
    p95_ms=float(os.getenv("SCOPEX_SLO_P95_MS", "500")),
    error_pct=float(os.getenv("SCOPEX_SLO_ERROR_PCT", "1")),
)
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(timeout=10.0)
    if os.getenv("STOREFRONT_AUTOSTART", "1") == "1":
        asyncio.get_running_loop().run_in_executor(None, testbed.apply, TestbedConfig())
    yield
    await _client.aclose()
    if runner.current is not None:
        runner.current.cancel = True
    await run_in_threadpool(testbed.stop)


app = FastAPI(title="Bazaaro", lifespan=lifespan)

# Optional write protection for public deployments: when LAB_TOKEN is set, anything that
# changes the testbed or starts/stops tests needs the header ``x-lab-token``.
LAB_TOKEN = os.getenv("LAB_TOKEN", "")
_PROTECTED_PREFIXES = ("/api/testbed/apply", "/api/lab/slo", "/api/lab/tests", "/api/lab/shield/")


@app.middleware("http")
async def lab_token_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
    if (
        LAB_TOKEN
        and request.method != "GET"
        and request.url.path.startswith(_PROTECTED_PREFIXES)
        and not hmac.compare_digest(request.headers.get("x-lab-token", ""), LAB_TOKEN)
    ):
        return JSONResponse({"detail": "Lab token required"}, status_code=401)
    return await call_next(request)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cart_id(request: Request, response: Response) -> str:
    cart_id = request.cookies.get(CART_COOKIE)
    if not cart_id:
        cart_id = f"web_{uuid.uuid4().hex[:12]}"
        response.set_cookie(CART_COOKIE, cart_id, httponly=True, samesite="lax")
    return cart_id


def _peer_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    return shield.client_ip(peer, request.headers.get("x-forwarded-for"))


def _decision_response(decision: Any) -> dict[str, Any]:
    """Turns a shield refusal into an HTTP error (with Retry-After when known)."""
    if not decision.allowed:
        headers = (
            {"Retry-After": f"{max(1, round(decision.retry_after))}"}
            if decision.retry_after
            else None
        )
        raise HTTPException(decision.status, decision.body(), headers=headers)
    return decision.detail


def _guard(request: Request, sid: str, action: str, product_id: str | None = None) -> None:
    """Runs the flash-sale shield for a purchase-path request; raises on refusal."""
    _decision_response(
        shield.guard(
            sid=sid,
            ip=_peer_ip(request),
            user_agent=request.headers.get("user-agent", ""),
            action=action,
            product_id=product_id,
            signals=Shield.parse_signals(request.headers.get("x-bz-signals")),
        )
    )


def _error_detail(resp: httpx.Response) -> str:
    if resp.headers.get("content-type", "").startswith("application/json"):
        return str(resp.json().get("detail", resp.text))
    return resp.text


async def _call(route: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
    assert _client is not None
    t0 = time.perf_counter()
    try:
        resp = await _client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        telemetry.record(route, (time.perf_counter() - t0) * 1000, False)
        raise HTTPException(503, f"{route}: service unavailable ({type(exc).__name__})") from exc
    telemetry.record(route, (time.perf_counter() - t0) * 1000, resp.status_code < 500)
    return resp


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# store API
# ---------------------------------------------------------------------------


@app.get("/api/categories")
def categories() -> list[dict[str, str]]:
    return [{"id": k, "name": v} for k, v in CATEGORIES.items()]


@app.get("/api/products")
async def products(
    q: str = "",
    category: str = "",
    sort: str = "featured",
    max_price: float | None = None,
    fast: bool = False,
    live: bool = False,
) -> list[dict[str, Any]]:
    """Catalog search; ``live=true`` adds each item's real stock from inventory."""
    items = [p.to_dict() for p in search(q, category, sort, max_price, fast)]
    if live:
        stocks = await asyncio.gather(*(_live_stock(i["id"]) for i in items))
        for item, stock in zip(items, stocks, strict=True):
            item["live_stock"] = stock
    return items


async def _live_stock(product_id: str) -> int | None:
    try:
        resp = await _call("inventory", "GET", f"{URLS['inventory']}/inventory/{product_id}")
    except HTTPException:
        return None
    return resp.json()["available_qty"] if resp.status_code == 200 else None


@app.get("/api/products/{product_id}")
async def product(product_id: str, request: Request, response: Response) -> dict[str, Any]:
    item = PRODUCTS_BY_ID.get(product_id)
    if not item:
        raise HTTPException(404, "Product not found")
    shield.note_view(_cart_id(request, response), product_id)
    data = item.to_dict()
    data["live_stock"] = None
    data["reserved"] = None
    data["max_per_customer"] = shield.config.max_per_customer if item.limited else None
    try:
        resp = await _call("inventory", "GET", f"{URLS['inventory']}/inventory/{product_id}")
        if resp.status_code == 200:
            data["live_stock"] = resp.json()["available_qty"]
            data["reserved"] = resp.json()["reserved_qty"]
    except HTTPException:
        pass
    return data


class AddToCart(BaseModel):
    product_id: str
    quantity: int = Field(default=1, ge=1, le=20)


@app.get("/api/cart")
async def get_cart(request: Request, response: Response) -> dict[str, Any]:
    cart_id = _cart_id(request, response)
    resp = await _call("cart", "GET", f"{URLS['cart']}/cart/{cart_id}")
    if resp.status_code != 200:
        return {"cart_id": cart_id, "items": [], "total_amount": 0.0}
    cart = resp.json()
    cart["reservations"] = shield.config.reservations
    cart["holds"] = await _holds(cart_id) if shield.config.reservations else {}
    return cart


async def _holds(cart_id: str) -> dict[str, Any]:
    try:
        resp = await _call("inventory", "GET", f"{URLS['inventory']}/inventory/holds/{cart_id}")
    except HTTPException:
        return {}
    if resp.status_code != 200:
        return {}
    return {line["product_id"]: line for line in resp.json()["lines"]}


async def _create_hold(cart_id: str, product_id: str, quantity: int) -> None:
    item = PRODUCTS_BY_ID[product_id]
    resp = await _call(
        "inventory",
        "POST",
        f"{URLS['inventory']}/inventory/holds",
        json={
            "hold_id": cart_id,
            "product_id": product_id,
            "quantity": quantity,
            "ttl_seconds": shield.config.hold_ttl_s,
            "max_per_hold": shield.config.max_per_customer if item.limited else None,
        },
    )
    if resp.status_code != 200:
        raise HTTPException(
            resp.status_code, {"code": "reservation_refused", "message": _error_detail(resp)}
        )


@app.post("/api/cart/items")
async def add_to_cart(body: AddToCart, request: Request, response: Response) -> dict[str, Any]:
    item = PRODUCTS_BY_ID.get(body.product_id)
    if not item:
        raise HTTPException(404, "Product not found")
    cart_id = _cart_id(request, response)
    _guard(request, cart_id, "add_to_cart", item.id)
    if shield.config.reservations:
        await _create_hold(cart_id, item.id, body.quantity)
    resp = await _call(
        "cart",
        "POST",
        f"{URLS['cart']}/cart/{cart_id}/items",
        json={
            "product_id": item.id,
            "product_name": item.name,
            "quantity": body.quantity,
            "unit_price": item.price,
        },
    )
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    cart = resp.json()
    if shield.config.reservations:
        cart["holds"] = await _holds(cart_id)
    return cart


@app.post("/api/cart/rehold")
async def rehold(request: Request, response: Response) -> dict[str, Any]:
    """Re-reserves cart lines whose hold expired (fails per line if stock is gone)."""
    cart_id = _cart_id(request, response)
    if not shield.config.reservations:
        return {"reserved": [], "failed": []}
    _guard(request, cart_id, "rehold")
    cart = (await _call("cart", "GET", f"{URLS['cart']}/cart/{cart_id}")).json()
    holds = await _holds(cart_id)
    reserved, failed = [], []
    for line in cart.get("items", []):
        have = holds.get(line["product_id"], {})
        missing = line["quantity"] - (have.get("quantity", 0) if have.get("seconds_left") else 0)
        if missing <= 0:
            continue
        try:
            await _create_hold(cart_id, line["product_id"], missing)
            reserved.append(line["product_id"])
        except HTTPException as exc:
            failed.append({"product_id": line["product_id"], "reason": str(exc.detail)})
    return {"reserved": reserved, "failed": failed}


@app.delete("/api/cart")
async def clear_cart(request: Request, response: Response) -> dict[str, str]:
    cart_id = _cart_id(request, response)
    await _call("cart", "DELETE", f"{URLS['cart']}/cart/{cart_id}")
    # Return reserved stock immediately rather than waiting for the TTL.
    await _call("inventory", "DELETE", f"{URLS['inventory']}/inventory/holds/{cart_id}")
    return {"status": "cleared"}


class CheckoutBody(BaseModel):
    name: str = "Guest"
    payment_method: str = "credit_card"


@app.post("/api/checkout")
async def checkout(body: CheckoutBody, request: Request, response: Response) -> dict[str, Any]:
    cart_id = _cart_id(request, response)
    _guard(request, cart_id, "checkout")
    t0 = time.perf_counter()
    resp = await _call(
        "checkout",
        "POST",
        f"{URLS['checkout']}/checkout",
        json={
            "cart_id": cart_id,
            "user_id": body.name or "guest",
            "payment_method": body.payment_method,
            "hold_id": cart_id if shield.config.reservations else None,
        },
    )
    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    if resp.status_code != 200:
        raise HTTPException(
            resp.status_code, {"message": _error_detail(resp), "latency_ms": elapsed}
        )
    data = resp.json()
    data["latency_ms"] = elapsed
    shield.room.release(cart_id)  # purchase done: give the slot to the next shopper
    return data


@app.get("/api/health")
def health() -> dict[str, Any]:
    snap = telemetry.snapshot(slo, runner.current)
    snap["testbed"] = testbed.status()["state"]
    snap["testbed_label"] = TESTBED_LABEL
    snap["write_protected"] = bool(LAB_TOKEN)
    snap["slo"] = slo.__dict__
    cfg = shield.config
    snap["shield"] = {
        "waiting_room": cfg.waiting_room,
        "reservations": cfg.reservations,
        "bot_defense": cfg.bot_defense,
        "room_open": shield.room.is_open(cfg, time.time()),
        "queue_length": len(shield.room.queue),
        "hold_ttl_s": cfg.hold_ttl_s,
    }
    return snap


# ---------------------------------------------------------------------------
# flash-sale shield (shopper-facing)
# ---------------------------------------------------------------------------


@app.post("/api/shield/join")
def shield_join(request: Request, response: Response) -> dict[str, Any]:
    return _decision_response(shield.join(_cart_id(request, response), _peer_ip(request)))


@app.get("/api/shield/queue")
def shield_queue(request: Request, response: Response) -> dict[str, Any]:
    return shield.queue_status(_cart_id(request, response))


@app.post("/api/shield/challenge")
def shield_challenge(request: Request, response: Response) -> dict[str, Any]:
    return shield.challenge(_cart_id(request, response))


class SolveBody(BaseModel):
    challenge: str = Field(max_length=64)
    nonce: str = Field(max_length=32)


@app.post("/api/shield/solve")
def shield_solve(body: SolveBody, request: Request, response: Response) -> dict[str, Any]:
    return _decision_response(shield.solve(_cart_id(request, response), body.challenge, body.nonce))


# ---------------------------------------------------------------------------
# testbed + lab API
# ---------------------------------------------------------------------------


@app.get("/api/testbed")
def testbed_status() -> dict[str, Any]:
    return testbed.status()


@app.post("/api/testbed/apply")
async def testbed_apply(body: dict[str, Any]) -> dict[str, Any]:
    if runner.busy():
        raise HTTPException(409, "A load test is running; cancel it before changing the testbed.")
    try:
        cfg = TestbedConfig.from_dict(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return await run_in_threadpool(testbed.apply, cfg)


class SLOBody(BaseModel):
    p95_ms: float = Field(gt=0, le=60000)
    error_pct: float = Field(ge=0, le=100)


@app.post("/api/lab/slo")
def set_slo(body: SLOBody) -> dict[str, float]:
    slo.p95_ms, slo.error_pct = body.p95_ms, body.error_pct
    return slo.__dict__


@app.get("/api/lab/constraints")
def lab_constraints() -> list[dict[str, Any]]:
    return load_constraint_registry()


@app.post("/api/lab/whatif")
async def lab_whatif(body: dict[str, Any]) -> dict[str, Any]:
    return await run_in_threadpool(what_if, WhatIfConfig.from_dict(body), slo)


class TestBody(BaseModel):
    kind: str = Field(pattern="^(steady|sweep|flash|drop)$")
    params: dict[str, Any] = Field(default_factory=dict)


def _validated_params(kind: str, p: dict[str, Any]) -> dict[str, Any]:
    share = min(1.0, max(0.0, float(p.get("checkout_share", 0.5))))
    if kind == "steady":
        return {
            "rps": min(800.0, max(1.0, float(p.get("rps", 20)))),
            "duration": min(120.0, max(3.0, float(p.get("duration", 15)))),
            "checkout_share": share,
        }
    if kind == "flash":
        return {
            "base_rps": min(200.0, max(1.0, float(p.get("base_rps", 5)))),
            "peak_rps": min(800.0, max(1.0, float(p.get("peak_rps", 80)))),
            "duration": min(60.0, max(3.0, float(p.get("duration", 10)))),
            "checkout_share": share,
        }
    levels = sorted({min(800.0, max(1.0, float(v))) for v in p.get("levels", [5, 10, 20, 40, 80])})
    return {
        "levels": levels[:12],
        "step_seconds": min(30.0, max(3.0, float(p.get("step_seconds", 6)))),
        "checkout_share": share,
    }


@app.post("/api/lab/tests")
async def start_test(body: TestBody, request: Request) -> dict[str, Any]:
    if runner.busy():
        raise HTTPException(409, "Another load test is already running.")
    status = testbed.status()
    if status["state"] != "running":
        raise HTTPException(409, f"Testbed is {status['state']}. Apply a config first.")
    if body.kind == "drop":
        test = LoadTest(id=uuid.uuid4().hex[:8], kind="drop", params=dict(body.params))
        runner.tests[test.id] = test
        runner.current = test
        test.status = "running"
        # Simulated shoppers must reach the store over loopback so their per-shopper
        # X-Forwarded-For IPs are trusted; behind a public proxy use SCOPEX_INTERNAL_URL.
        base = os.getenv("SCOPEX_INTERNAL_URL") or str(request.base_url)
        asyncio.create_task(run_drop(test, runner, shield, base, status["config"]))
        return test.to_dict()
    test = LoadTest(
        id=uuid.uuid4().hex[:8], kind=body.kind, params=_validated_params(body.kind, body.params)
    )
    runner.tests[test.id] = test
    asyncio.create_task(runner.run(test, status["config"], SLO(slo.p95_ms, slo.error_pct)))
    return test.to_dict()


@app.get("/api/lab/tests/{test_id}")
def get_test(test_id: str) -> dict[str, Any]:
    test = runner.tests.get(test_id)
    if not test:
        raise HTTPException(404, "Unknown test")
    return test.to_dict()


@app.post("/api/lab/tests/{test_id}/cancel")
def cancel_test(test_id: str) -> dict[str, str]:
    test = runner.tests.get(test_id)
    if not test:
        raise HTTPException(404, "Unknown test")
    test.cancel = True
    return {"status": "cancelling"}


@app.get("/api/lab/history")
def history() -> list[dict[str, Any]]:
    return list(runner.history)


@app.get("/api/lab/shield")
def lab_shield() -> dict[str, Any]:
    return shield.status()


@app.post("/api/lab/shield/config")
def lab_shield_config(body: dict[str, Any]) -> dict[str, Any]:
    if runner.busy():
        raise HTTPException(409, "A test is running; wait for it to finish.")
    try:
        shield.configure(body)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return shield.status()


@app.post("/api/lab/shield/reset")
def lab_shield_reset() -> dict[str, Any]:
    shield.reset_state()
    return shield.status()
