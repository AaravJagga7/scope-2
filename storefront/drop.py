"""Flash-sale drop test: shield off vs on, measured on the local testbed.

Simulated shoppers and bots go through the storefront's public HTTP API (the
same path as the browser), so the shield, the TTL holds in inventory and the
checkout service are all exercised for real.

* Humans arrive as a Poisson process, view the product, think, add one unit,
  think again, and check out. A share of them abandon their cart after adding,
  which is what TTL holds are for. When the waiting room asks them to queue they
  solve the proof-of-work (as the browser would), join and poll until admitted
  or out of patience.
* Bots skip the product page, drop cookies between attempts, send no
  interaction signals and hammer add-to-cart + checkout. Half use an automation
  user-agent, half fake a browser one. Optionally they also solve proof-of-work
  (a determined bot can; it just costs CPU time).

Each arm starts from a known baseline: the drop item's stock is reset and all
of its holds are deleted; the shield state is cleared. Results are labelled
"validated on local testbed". Caveat: the simulated clients run in the same
process and on the same machine as the store, so absolute latencies include
that contention.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from scopex.storage.repository import get_current_git_commit
from storefront.catalog import PRODUCTS_BY_ID
from storefront.lab import TESTBED_LABEL
from storefront.shield import Shield, solve_pow
from storefront.testbed import URLS

DROP_PRODUCT = "bz-701"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "data" / "experiments"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)
BOT_UAS = ["python-httpx/0.27.0", BROWSER_UA]


@dataclass
class DropParams:
    stock: int = 50
    humans: int = 200
    bots: int = 10
    bot_rps: float = 8.0  # attempts per second per bot
    arrival_s: float = 10.0
    hold_ttl_s: float = 20.0
    abandon_pct: float = 0.25
    patience_s: float = 45.0
    bots_solve_pow: bool = False
    arms: str = "both"  # off | on | both
    seed: int = 7

    @classmethod
    def from_dict(cls, p: dict[str, Any]) -> DropParams:
        def clamp(v: Any, lo: float, hi: float) -> float:
            return min(hi, max(lo, float(v)))

        d = cls()
        return cls(
            stock=int(clamp(p.get("stock", d.stock), 1, 500)),
            humans=int(clamp(p.get("humans", d.humans), 1, 600)),
            bots=int(clamp(p.get("bots", d.bots), 0, 50)),
            bot_rps=clamp(p.get("bot_rps", d.bot_rps), 0.5, 40),
            arrival_s=clamp(p.get("arrival_s", d.arrival_s), 1, 60),
            hold_ttl_s=clamp(p.get("hold_ttl_s", d.hold_ttl_s), 5, 600),
            abandon_pct=clamp(p.get("abandon_pct", d.abandon_pct), 0, 1),
            patience_s=clamp(p.get("patience_s", d.patience_s), 5, 180),
            bots_solve_pow=bool(p.get("bots_solve_pow", d.bots_solve_pow)),
            arms=str(p.get("arms", d.arms)) if p.get("arms") in ("off", "on", "both") else "both",
            seed=int(p.get("seed", d.seed)),
        )


@dataclass
class ArmStats:
    arm: str
    humans: dict[str, int] = field(default_factory=dict)
    human_checkout_ms: list[float] = field(default_factory=list)
    human_add_ms: list[float] = field(default_factory=list)
    queue_waits: list[float] = field(default_factory=list)
    bought_humans: int = 0
    bought_bots: int = 0
    bot_attempts: int = 0
    bot_outcomes: dict[str, int] = field(default_factory=dict)
    server_errors: int = 0
    responses: int = 0
    sold_out_at: float | None = None

    def bump(self, table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1


def _pct(values: list[float], q: float) -> float | None:
    return round(float(np.percentile(values, q)), 1) if values else None


def _body(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    detail = data.get("detail", data) if isinstance(data, dict) else {}
    return detail if isinstance(detail, dict) else {"message": str(detail)}


class DropSimulator:
    def __init__(self, base_url: str, shield: Shield, params: DropParams) -> None:
        self.base = base_url.rstrip("/")
        self.shield = shield
        self.p = params
        self.rng = random.Random(params.seed)

    # -- plumbing ------------------------------------------------------------

    async def _req(
        self,
        client: httpx.AsyncClient,
        stats: ArmStats,
        method: str,
        path: str,
        headers: dict[str, str],
        **kw: Any,
    ) -> httpx.Response | None:
        try:
            resp = await client.request(method, self.base + path, headers=headers, **kw)
        except httpx.HTTPError:
            stats.server_errors += 1
            stats.responses += 1
            return None
        stats.responses += 1
        if resp.status_code >= 500:
            stats.server_errors += 1
        return resp

    async def _solve(self, client, stats, headers, challenge: dict[str, Any]) -> bool:
        nonce = await asyncio.to_thread(solve_pow, challenge["challenge"], int(challenge["bits"]))
        resp = await self._req(
            client,
            stats,
            "POST",
            "/api/shield/solve",
            headers,
            json={"challenge": challenge["challenge"], "nonce": str(nonce)},
        )
        return resp is not None and resp.status_code == 200

    async def _queue(self, client, stats, headers, deadline: float) -> str:
        """Joins the waiting room and waits for admission. Returns the outcome."""
        joined = time.time()
        for _ in range(4):
            resp = await self._req(client, stats, "POST", "/api/shield/join", headers)
            if resp is None:
                return "error"
            body = _body(resp)
            if resp.status_code == 200:
                break
            if body.get("code") == "challenge_required":
                if not await self._solve(client, stats, headers, body):
                    return "challenge_failed"
                continue
            if resp.status_code == 429:
                await asyncio.sleep(float(resp.headers.get("retry-after", 1)))
                continue
            return "error"
        while time.time() < deadline:
            resp = await self._req(client, stats, "GET", "/api/shield/queue", headers)
            if (
                resp is not None
                and resp.status_code == 200
                and resp.json().get("state") == "admitted"
            ):
                stats.queue_waits.append(time.time() - joined)
                return "admitted"
            if (
                resp is not None
                and resp.status_code == 200
                and resp.json().get("state") == "not_queued"
            ):
                await self._req(client, stats, "POST", "/api/shield/join", headers)
            await asyncio.sleep(1.0)
        return "gave_up_in_queue"

    async def _guarded(self, client, stats, headers, method, path, deadline, **kw):
        """Sends a purchase-path request, handling queue / challenge / rate-limit replies."""
        for _ in range(8):
            t0 = time.perf_counter()
            resp = await self._req(client, stats, method, path, headers, **kw)
            ms = (time.perf_counter() - t0) * 1000
            if resp is None:
                return "error", None, ms
            body = _body(resp)
            code = body.get("code")
            if resp.status_code == 200:
                return "ok", resp, ms
            if code == "queue_required":
                outcome = await self._queue(client, stats, headers, deadline)
                if outcome != "admitted":
                    return outcome, resp, ms
                continue
            if code == "challenge_required":
                if not await self._solve(client, stats, headers, body):
                    return "challenge_failed", resp, ms
                continue
            if resp.status_code == 429:
                await asyncio.sleep(float(resp.headers.get("retry-after", 1)))
                continue
            if code == "blocked":
                return "blocked", resp, ms
            if resp.status_code == 409:
                return "conflict", resp, ms
            return "error", resp, ms
        return "error", None, 0.0

    # -- actors --------------------------------------------------------------

    async def human(self, client, stats: ArmStats, idx: int, start_at: float) -> None:
        await asyncio.sleep(max(0.0, start_at - time.time()))
        rng = random.Random(self.p.seed * 1000 + idx)
        born = time.time()
        headers = {
            "user-agent": BROWSER_UA,
            "x-forwarded-for": f"10.1.{idx // 250}.{idx % 250 + 1}",
            "cookie": f"bz_cart=sim_h_{stats.arm}_{self.p.seed}_{idx}_{int(born)}",
        }
        deadline = born + self.p.patience_s

        def signals() -> dict[str, str]:
            return {
                **headers,
                "x-bz-signals": json.dumps(
                    {
                        "p": rng.randint(20, 400),
                        "k": rng.randint(0, 5),
                        "t": (time.time() - born) * 1000,
                    }
                ),
            }

        await self._req(client, stats, "GET", f"/api/products/{DROP_PRODUCT}", headers)
        await asyncio.sleep(rng.uniform(0.8, 2.5))
        outcome, resp, ms = await self._guarded(
            client,
            stats,
            signals(),
            "POST",
            "/api/cart/items",
            deadline,
            json={"product_id": DROP_PRODUCT, "quantity": 1},
        )
        if outcome != "ok":
            stats.bump(stats.humans, "sold_out" if outcome == "conflict" else outcome)
            return
        stats.human_add_ms.append(ms)
        if rng.random() < self.p.abandon_pct:
            stats.bump(stats.humans, "abandoned_cart")
            return
        await asyncio.sleep(rng.uniform(1.0, 3.0))
        outcome, resp, ms = await self._guarded(
            client,
            stats,
            signals(),
            "POST",
            "/api/checkout",
            time.time() + self.p.patience_s,
            json={"name": f"Human {idx}"},
        )
        if outcome == "ok":
            stats.human_checkout_ms.append(ms)
            stats.bought_humans += 1
            stats.bump(stats.humans, "bought")
        else:
            stats.bump(stats.humans, "checkout_rejected" if outcome == "conflict" else outcome)

    async def bot(self, client, stats: ArmStats, idx: int, stop: asyncio.Event) -> None:
        rng = random.Random(self.p.seed * 7919 + idx)
        ua = BOT_UAS[idx % 2]
        ip = f"10.9.0.{idx + 1}"
        while not stop.is_set():
            sid = f"sim_b_{idx}_{rng.getrandbits(40):x}"  # cookie dropping
            headers = {"user-agent": ua, "x-forwarded-for": ip, "cookie": f"bz_cart={sid}"}
            stats.bot_attempts += 1
            outcome = await self._bot_attempt(client, stats, headers)
            stats.bump(stats.bot_outcomes, outcome)
            if outcome == "bought":
                stats.bought_bots += 1
            await asyncio.sleep(1.0 / self.p.bot_rps)

    async def _bot_attempt(self, client, stats, headers) -> str:
        add = await self._req(
            client,
            stats,
            "POST",
            "/api/cart/items",
            headers,
            json={"product_id": DROP_PRODUCT, "quantity": 2},
        )
        if add is None:
            return "error"
        body = _body(add)
        if (
            add.status_code != 200
            and self.p.bots_solve_pow
            and body.get("code") == "challenge_required"
        ):
            if await self._solve(client, stats, headers, body):
                add = await self._req(
                    client,
                    stats,
                    "POST",
                    "/api/cart/items",
                    headers,
                    json={"product_id": DROP_PRODUCT, "quantity": 2},
                )
                body = _body(add) if add is not None else {}
        if add is None or add.status_code != 200:
            return body.get("code") or (
                "sold_out" if add is not None and add.status_code == 409 else "rejected"
            )
        chk = await self._req(client, stats, "POST", "/api/checkout", headers, json={"name": "bot"})
        if chk is not None and chk.status_code == 200:
            return "bought"
        return (_body(chk).get("code") if chk is not None else None) or "checkout_failed"

    # -- arm -----------------------------------------------------------------

    async def _reset(self, client: httpx.AsyncClient) -> None:
        resp = await client.post(
            f"{URLS['inventory']}/inventory/admin/reset",
            json={"product_id": DROP_PRODUCT, "available_qty": self.p.stock},
        )
        resp.raise_for_status()
        self.shield.reset_state()

    async def _sample(self, client: httpx.AsyncClient) -> dict[str, Any]:
        try:
            resp = await client.get(f"{URLS['inventory']}/inventory/holds-stats/{DROP_PRODUCT}")
            return resp.json() if resp.status_code == 200 else {}
        except httpx.HTTPError:
            return {}

    async def run_arm(self, arm: str, test: Any, t_offset: float) -> dict[str, Any]:
        stats = ArmStats(arm=arm)
        limits = httpx.Limits(max_connections=600, max_keepalive_connections=200)
        async with httpx.AsyncClient(timeout=15.0, limits=limits) as client:
            await self._reset(client)
            start = time.time()
            arrivals, t = [], 0.0
            rng = random.Random(self.p.seed)
            for _ in range(self.p.humans):
                t += rng.expovariate(self.p.humans / self.p.arrival_s)
                arrivals.append(start + min(t, self.p.arrival_s))
            stop = asyncio.Event()
            bots = [
                asyncio.create_task(self.bot(client, stats, i, stop)) for i in range(self.p.bots)
            ]
            humans = [
                asyncio.create_task(self.human(client, stats, i, arrivals[i]))
                for i in range(self.p.humans)
            ]
            humans_done = asyncio.gather(*humans)
            hard_stop = start + self.p.arrival_s + self.p.patience_s * 2 + 30
            prev = {"h": 0, "b": 0, "ck": 0}
            tail_until: float | None = None
            while not test.cancel:
                await asyncio.sleep(1.0)
                now = time.time()
                inv = await self._sample(client)
                sh = self.shield.status(now)
                ck = stats.human_checkout_ms[prev["ck"] :]
                sold = stats.bought_humans + stats.bought_bots * 2
                if stats.sold_out_at is None and sold >= self.p.stock:
                    stats.sold_out_at = round(now - start, 1)
                test.seconds.append(
                    {
                        "t": round(t_offset + now - start, 1),
                        "arm": arm,
                        "available": inv.get("available_qty"),
                        "held": (
                            inv.get("by_status", {}).get("held", {}).get("units", 0)
                            + inv.get("by_status", {}).get("locked", {}).get("units", 0)
                        ),
                        "expired_units": inv.get("by_status", {})
                        .get("expired", {})
                        .get("units", 0),
                        "sold": sold,
                        "sold_humans": stats.bought_humans,
                        "sold_bots_units": stats.bought_bots * 2,
                        "queue": sh["queue_length"],
                        "active_shoppers": sh["active_shoppers"],
                        "room_open": sh["room_open"],
                        "checkout_p95_ms": _pct(ck, 95) or 0.0,
                        "error_pct": round(100 * stats.server_errors / stats.responses, 2)
                        if stats.responses
                        else 0.0,
                        "offered_rps": round(sh["purchase_rps"], 1),
                        "inflight": sum(1 for h in humans if not h.done()),
                    }
                )
                prev = {
                    "h": stats.bought_humans,
                    "b": stats.bought_bots,
                    "ck": len(stats.human_checkout_ms),
                }
                if humans_done.done() and not stop.is_set():
                    stop.set()
                    # Keep sampling until held stock has had time to expire and return.
                    tail_until = now + (self.p.hold_ttl_s + 3 if arm == "on" else 2)
                if tail_until is not None and now >= tail_until:
                    break
                if now > hard_stop:
                    break
            stop.set()
            for task in humans + bots:
                task.cancel()
            await asyncio.gather(*humans, *bots, return_exceptions=True)
            final = await self._sample(client)
        return self._arm_summary(stats, final, start)

    def _arm_summary(self, s: ArmStats, inv: dict[str, Any], start: float) -> dict[str, Any]:
        units_sold = s.bought_humans + 2 * s.bought_bots
        by_status = inv.get("by_status", {})
        sh = self.shield.status()
        attempted_humans = self.p.humans - s.humans.get("abandoned_cart", 0)
        return {
            "arm": s.arm,
            "stock": self.p.stock,
            "units_sold": units_sold,
            "oversold_units": max(0, units_sold - self.p.stock),
            "inventory_available_end": inv.get("available_qty"),
            "inventory_reserved_end": inv.get("reserved_qty"),
            "units_to_humans": s.bought_humans,
            "units_to_bots": 2 * s.bought_bots,
            "bot_share_pct": round(100 * 2 * s.bought_bots / units_sold, 1) if units_sold else 0.0,
            "humans": dict(sorted(s.humans.items())),
            "human_success_pct": round(100 * s.bought_humans / attempted_humans, 1)
            if attempted_humans
            else 0.0,
            "human_checkout_p50_ms": _pct(s.human_checkout_ms, 50),
            "human_checkout_p95_ms": _pct(s.human_checkout_ms, 95),
            "human_add_p95_ms": _pct(s.human_add_ms, 95),
            "queue_wait_p50_s": _pct(s.queue_waits, 50),
            "queue_wait_p95_s": _pct(s.queue_waits, 95),
            "max_queue": sh["max_queue"],
            "bot_attempts": s.bot_attempts,
            "bot_outcomes": dict(sorted(s.bot_outcomes.items())),
            "shield_counters": sh["counters"],
            "expired_hold_units_returned": by_status.get("expired", {}).get("units", 0),
            "server_error_pct": round(100 * s.server_errors / s.responses, 2)
            if s.responses
            else 0.0,
            "responses": s.responses,
            "sold_out_after_s": s.sold_out_at,
            "duration_s": round(time.time() - start, 1),
        }


ARM_LAYERS = {
    "off": {"waiting_room": False, "reservations": False, "bot_defense": False},
    "on": {"waiting_room": True, "reservations": True, "bot_defense": True},
}


async def run_drop(
    test: Any, runner: Any, shield: Shield, base_url: str, testbed_config: dict
) -> None:
    """Runs the drop test arms and stores the comparison on ``test.result``."""
    params = DropParams.from_dict(test.params)
    test.params = params.__dict__
    runner.current = test
    test.status = "running"
    test.started_at = time.time()
    saved = shield.config
    try:
        if PRODUCTS_BY_ID.get(DROP_PRODUCT) is None:
            raise RuntimeError(f"{DROP_PRODUCT} missing from catalog")
        sim = DropSimulator(base_url, shield, params)
        arms = ["off", "on"] if params.arms == "both" else [params.arms]
        results, t = [], 0.0
        for arm in arms:
            if test.cancel:
                break
            shield.config = saved.updated({**ARM_LAYERS[arm], "hold_ttl_s": params.hold_ttl_s})
            summary = await sim.run_arm(arm, test, t)
            summary["shield_config"] = shield.config.__dict__.copy()
            results.append(summary)
            test.steps.append(summary)
            t = (test.seconds[-1]["t"] + 2) if test.seconds else t
        test.result = {
            "label": TESTBED_LABEL,
            "drop_product": DROP_PRODUCT,
            "params": params.__dict__,
            "testbed_config": testbed_config,
            "arms": results,
            "caveat": (
                "Simulated clients share the store's process and machine; latencies include "
                "that contention. Bots are scripted with the behaviours described in drop.py."
            ),
        }
        test.status = "cancelled" if test.cancel else "done"
        test.result["saved_to"] = save_drop_result(test)
    except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
        test.status = "error"
        test.error = f"{type(exc).__name__}: {exc}"
    finally:
        shield.config = saved
        shield.reset_state()
        test.finished_at = time.time()
        runner.history.appendleft(
            {
                "id": test.id,
                "kind": test.kind,
                "status": test.status,
                "config": testbed_config,
                "result": test.result,
                "finished_at": test.finished_at,
            }
        )


def save_drop_result(test: Any) -> str:
    """Stores config + seed + timestamps + git commit + per-second series (reproducibility)."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(test.started_at))
    path = RESULTS_DIR / f"drop_test_{stamp}_{test.id}.json"
    record = {
        "experiment": "flash_sale_drop",
        "label": TESTBED_LABEL,
        "test_id": test.id,
        "started_at": test.started_at,
        "finished_at": time.time(),
        "git_commit": get_current_git_commit(),
        "params": test.params,
        "result": test.result,
        "seconds": test.seconds,
    }
    path.write_text(json.dumps(record, indent=1, default=str))
    return str(path.relative_to(RESULTS_DIR.parent.parent))
