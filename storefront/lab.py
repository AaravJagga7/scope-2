"""Constraint Lab: live constraint tests on the local testbed + model what-if.

Two kinds of result, always labelled:

* ``validated on local testbed`` - real HTTP load against the running
  microservices (open-loop arrivals, per-second p50/p95/error, requested vs
  achieved rps). These are single-machine numbers: SQLite instead of Postgres,
  uvicorn workers instead of pods.
* ``model (simulated)`` - the SCOPE-X queuing model plus the Task 14D CPU
  capacity model, evaluated for a cluster configuration you choose. Stored
  constraints are evaluated against that configuration and shown with the class
  they earned in each scaling mode (static / HPA / vertical).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from scopex.discovery.hpa_transfer import _compile_evaluator
from scopex.discovery.vertical_transfer import cpu_capacity
from scopex.storage.config import ExperimentConfig, WorkloadSpec
from scopex.storage.dataset import simulate_system_metrics
from storefront.catalog import PRODUCTS
from storefront.testbed import URLS

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
# Honesty label for live results; set e.g. "validated on cloud testbed (Railway)" when deployed.
TESTBED_LABEL = os.getenv("SCOPEX_TESTBED_LABEL", "validated on local testbed")
CONNECTIONS_PER_REPLICA = 6.0


@dataclass
class SLO:
    """Configurable defaults; the lab reports against them, it does not assume them."""

    p95_ms: float = 500.0
    error_pct: float = 1.0


# ---------------------------------------------------------------------------
# Constraint registry (reads the repository's stored results)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_constraint_registry() -> list[dict[str, Any]]:
    """Merges the static constraint set with its HPA (14C) and vertical (14D) classes."""
    static = _read_json(DATA_DIR / "constraints_summary.json") or {}
    hpa = _read_json(DATA_DIR / "hpa_transfer_summary.json") or {}
    vertical = _read_json(DATA_DIR / "vertical_transfer_summary.json") or {}

    hpa_by_id = {c["constraint_id"]: c for c in hpa.get("constraints", [])}
    vert_by_id = {c["constraint_id"]: c for c in vertical.get("constraints", [])}

    registry: list[dict[str, Any]] = []
    for idx, item in enumerate(static.get("top_constraints", []), start=1):
        cid = f"C{idx:02d}"
        registry.append(
            {
                "id": cid,
                "kind": "learned",
                "rule": item["inequality"],
                "method": item.get("method", ""),
                "static_precision": item.get("heldout_precision"),
                "static_recall": item.get("heldout_recall"),
                "classes": {
                    "static": "validated",
                    "hpa": hpa_by_id.get(cid, {}).get("classification", "untested"),
                    "vertical": vert_by_id.get(cid, {}).get("classification", "untested"),
                },
                "evidence": {
                    "hpa": hpa_by_id.get(cid, {}).get("evidence", ""),
                    "vertical": vert_by_id.get(cid, {}).get("evidence", ""),
                },
            }
        )
    registry.append(
        {
            "id": "K01",
            "kind": "structural",
            "rule": f"checkout_replicas * {CONNECTIONS_PER_REPLICA:g} - db_pool_size > 0",
            "method": "invariant",
            "static_precision": None,
            "static_recall": None,
            "classes": {
                "static": "validated",
                "hpa": hpa_by_id.get("K01", {}).get("classification", "untested"),
                "vertical": "untested",
            },
            "evidence": {"hpa": hpa_by_id.get("K01", {}).get("evidence", ""), "vertical": ""},
        }
    )
    for cid in ("K02", "K03"):
        rec = vert_by_id.get(cid)
        if rec:
            registry.append(
                {
                    "id": cid,
                    "kind": "mechanism",
                    "rule": rec["rule"],
                    "method": "mechanism_threshold",
                    "static_precision": rec.get("baseline_static_precision"),
                    "static_recall": None,
                    "classes": {
                        "static": "validated",
                        "hpa": "untested",
                        "vertical": rec["classification"],
                    },
                    "evidence": {"hpa": "", "vertical": rec.get("evidence", "")},
                    "class_by_cpu": rec.get("class_by_cpu", {}),
                }
            )
    for i, rec in enumerate(vertical.get("new_constraints", []), start=1):
        registry.append(
            {
                "id": f"V{i:02d}",
                "kind": "new (vertical)",
                "rule": rec["inequality"],
                "method": rec.get("method", ""),
                "static_precision": None,
                "static_recall": None,
                "classes": {"static": "untested", "hpa": "untested", "vertical": "new"},
                "evidence": {
                    "hpa": "",
                    "vertical": f"held-out precision {rec.get('heldout_precision')}",
                },
            }
        )
    for i, rec in enumerate(hpa.get("new_transient_constraints", []), start=1):
        registry.append(
            {
                "id": f"N{i:02d}",
                "kind": "new (transient)",
                "rule": rec["inequality"],
                "method": rec.get("method", ""),
                "static_precision": None,
                "static_recall": None,
                "classes": {"static": "untested", "hpa": "new (transient)", "vertical": "untested"},
                "evidence": {"hpa": "learned on HPA transition rows", "vertical": ""},
            }
        )
    alpha = (vertical.get("capacity_fit") or {}).get("alpha")
    for rec in registry:
        rec["alpha"] = alpha
    return registry


# ---------------------------------------------------------------------------
# Model what-if (simulated)
# ---------------------------------------------------------------------------


@dataclass
class WhatIfConfig:
    replicas: int = 4
    cpu_cores: float = 0.5
    memory_mib: int = 1024
    db_pool_size: int = 50
    cache_enabled: bool = True
    target_rps: float = 400.0
    workload: str = "flash_sale"
    mode: str = "static"  # static | hpa | vertical

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WhatIfConfig:
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        cfg.replicas = int(min(10, max(1, int(cfg.replicas))))
        cfg.cpu_cores = float(min(4.0, max(0.25, float(cfg.cpu_cores))))
        cfg.memory_mib = int(cfg.memory_mib)
        cfg.db_pool_size = int(min(200, max(1, int(cfg.db_pool_size))))
        cfg.cache_enabled = bool(cfg.cache_enabled)
        cfg.target_rps = float(min(2000, max(1, float(cfg.target_rps))))
        if cfg.mode not in ("static", "hpa", "vertical"):
            cfg.mode = "static"
        return cfg


def _model_point(
    cfg: WhatIfConfig, rps: float, replicas: int | None = None, cpu: float | None = None
) -> dict[str, float]:
    reps = replicas if replicas is not None else cfg.replicas
    cores = cpu if cpu is not None else cfg.cpu_cores
    cap = cpu_capacity(cores)
    p95s, errs, ach = [], [], []
    for seed in (11, 23, 37):
        ec = ExperimentConfig(
            workload=WorkloadSpec(rps=max(1, int(round(rps))), profile=cfg.workload, duration=1),
            checkout_replicas=reps,
            db_pool_size=cfg.db_pool_size,
            cache_enabled=cfg.cache_enabled,
            seed=seed,
        )
        m = simulate_system_metrics(ec, seed=seed, frontend_rps_per_replica=cap)
        p95s.append(float(m["client_side_latency_percentiles"]["p95"]))
        errs.append(float(m["error_rate_percent"]))
        ach.append(float(m["achieved_rps"]))
    return {
        "p95_ms": float(np.mean(p95s)),
        "error_pct": float(np.mean(errs)),
        "achieved_rps": float(np.mean(ach)),
    }


def _max_safe_rps(
    cfg: WhatIfConfig, slo: SLO, replicas: int | None = None, cpu: float | None = None
) -> float:
    """Largest offered load the model keeps within SLO (bisection on rps)."""
    lo, hi = 1.0, 3000.0

    def ok(r: float) -> bool:
        p = _model_point(cfg, r, replicas, cpu)
        return p["p95_ms"] <= slo.p95_ms and p["error_pct"] <= slo.error_pct

    if not ok(lo):
        return 0.0
    if ok(hi):
        return hi
    for _ in range(18):
        mid = 0.5 * (lo + hi)
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return round(lo, 1)


def _row_for(cfg: WhatIfConfig, rps: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "target_rps": rps,
                "checkout_replicas": cfg.replicas,
                "db_pool_size": cfg.db_pool_size,
                "cache_enabled": int(cfg.cache_enabled),
                "cpu_cores": cfg.cpu_cores,
                "memory_mib": cfg.memory_mib,
                "rps_per_replica": rps / max(1, cfg.replicas),
                "rps_per_pool_conn": rps / max(1, cfg.db_pool_size),
            }
        ]
    )


def _validity_for_mode(cls: str) -> str:
    if cls in ("validated", "unchanged", "new", "new (transient)"):
        return "valid"
    if cls == "shifted":
        return "shifted"
    if cls in ("violated-by-autoscaler",):
        return "violated"
    if cls == "disappeared":
        return "not observed"
    return "untested"


def what_if(cfg: WhatIfConfig, slo: SLO | None = None) -> dict[str, Any]:
    slo = slo or SLO()
    point = _model_point(cfg, cfg.target_rps)
    frontend_cap = cfg.replicas * cpu_capacity(cfg.cpu_cores)
    db_cap = (
        cfg.db_pool_size
        * 16.0
        * ((5.0 if cfg.workload == "viral_product" else 2.2) if cfg.cache_enabled else 1.0)
    )
    bottleneck = "CPU (checkout frontend)" if frontend_cap <= db_cap else "Database connection pool"

    curve_max = max(200.0, min(2000.0, 1.6 * min(frontend_cap, db_cap)))
    curve = []
    for rps in np.linspace(max(10.0, curve_max / 40), curve_max, 40):
        p = _model_point(cfg, float(rps))
        curve.append({"rps": round(float(rps), 1), **{k: round(v, 2) for k, v in p.items()}})

    by_replicas = [
        {"replicas": r, "max_safe_rps": _max_safe_rps(cfg, slo, replicas=r)} for r in range(1, 11)
    ]
    by_cpu = [
        {"cpu_cores": c, "max_safe_rps": _max_safe_rps(cfg, slo, cpu=c)}
        for c in (0.5, 1.0, 2.0, 4.0)
    ]
    max_safe = _max_safe_rps(cfg, slo)

    row = _row_for(cfg, cfg.target_rps)
    constraints = []
    for rec in load_constraint_registry():
        try:
            fires = bool(_compile_evaluator(rec["rule"])(row)[0])
        except Exception:  # noqa: BLE001 - rule may reference columns absent here
            fires = False
        mode_class = rec["classes"].get(cfg.mode, "untested")
        constraints.append(
            {
                "id": rec["id"],
                "kind": rec["kind"],
                "rule": rec["rule"],
                "fires": fires,
                "mode_class": mode_class,
                "validity": _validity_for_mode(mode_class),
                "classes": rec["classes"],
                "evidence": rec["evidence"].get(cfg.mode, ""),
            }
        )

    max_replicas_by_pool = math.floor(cfg.db_pool_size / CONNECTIONS_PER_REPLICA)
    warnings = []
    if cfg.replicas * CONNECTIONS_PER_REPLICA > cfg.db_pool_size:
        warnings.append(
            f"K01: {cfg.replicas} replicas x {CONNECTIONS_PER_REPLICA:g} connections = "
            f"{cfg.replicas * CONNECTIONS_PER_REPLICA:g} > pool {cfg.db_pool_size}. "
            f"Cap replicas (or HPA max) at {max_replicas_by_pool}."
        )
    if cfg.memory_mib <= 512 and cfg.target_rps > frontend_cap:
        warnings.append(
            "Overload at 512Mi: the 14D study saw OOM restarts in this regime (queued requests "
            "exhaust the memory limit)."
        )
    return {
        "label": "model (simulated)",
        "config": cfg.__dict__,
        "slo": slo.__dict__,
        "prediction": {k: round(v, 2) for k, v in point.items()},
        "slo_met": point["p95_ms"] <= slo.p95_ms and point["error_pct"] <= slo.error_pct,
        "capacity": {
            "per_replica_rps": round(cpu_capacity(cfg.cpu_cores), 1),
            "frontend_rps": round(frontend_cap, 1),
            "database_rps": round(db_cap, 1),
            "bottleneck": bottleneck,
            "max_safe_rps": max_safe,
            "headroom_pct": round(100 * (1 - cfg.target_rps / max_safe), 1) if max_safe else -100.0,
            "max_replicas_by_pool": max_replicas_by_pool,
        },
        "curve": curve,
        "boundary_by_replicas": by_replicas,
        "boundary_by_cpu": by_cpu,
        "constraints": constraints,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Live load tests on the local testbed
# ---------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q)) if values else 0.0


@dataclass
class LoadTest:
    id: str
    kind: str  # steady | sweep | flash
    params: dict[str, Any]
    status: str = "pending"
    started_at: float = 0.0
    finished_at: float | None = None
    seconds: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    cancel: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": TESTBED_LABEL,
            "params": self.params,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "seconds": self.seconds,
            "steps": self.steps,
            "result": self.result,
            "error": self.error,
        }


class LoadRunner:
    """Open-loop load generator against the real checkout and product services."""

    MAX_INFLIGHT = 800
    #: Scarce items would sell out and turn capacity tests into stock-out tests.
    LOAD_PRODUCTS = [p for p in PRODUCTS if p.stock >= 1000]

    def __init__(self) -> None:
        self.tests: dict[str, LoadTest] = {}
        self.current: LoadTest | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=20)

    def busy(self) -> bool:
        return self.current is not None and self.current.status == "running"

    async def _one(self, client: httpx.AsyncClient, kind: str) -> tuple[float, bool]:
        product = random.choice(self.LOAD_PRODUCTS)
        t0 = time.perf_counter()
        try:
            if kind == "browse":
                resp = await client.get(f"{URLS['product']}/products")
                ok = resp.status_code == 200
            else:
                cart_id = f"load_{uuid.uuid4().hex[:10]}"
                add = await client.post(
                    f"{URLS['cart']}/cart/{cart_id}/items",
                    json={
                        "product_id": product.id,
                        "product_name": product.name,
                        "quantity": 1,
                        "unit_price": product.price,
                    },
                )
                if add.status_code != 200:
                    return (time.perf_counter() - t0) * 1000, False
                resp = await client.post(
                    f"{URLS['checkout']}/checkout", json={"cart_id": cart_id, "user_id": "loadgen"}
                )
                ok = resp.status_code == 200
        except httpx.HTTPError:
            ok = False
        return (time.perf_counter() - t0) * 1000, ok

    async def _phase(
        self,
        test: LoadTest,
        client: httpx.AsyncClient,
        rps: float,
        duration: float,
        checkout_share: float,
        t_offset: float,
    ) -> dict[str, Any]:
        """Runs *duration* seconds at *rps* open-loop; appends per-second stats."""
        samples: list[tuple[int, str, float, bool]] = []
        inflight: set[asyncio.Task] = set()
        dropped = 0
        start = time.perf_counter()
        interval = 1.0 / rps
        sent = 0
        bucket_next = 1

        async def fire(kind: str) -> None:
            lat, ok = await self._one(client, kind)
            sec = int(time.perf_counter() - start)
            samples.append((sec, kind, lat, ok))

        def flush(upto: int) -> None:
            nonlocal bucket_next
            while bucket_next <= upto:
                sec = bucket_next - 1
                rows = [s for s in samples if s[0] == sec]
                lats_c = [s[2] for s in rows if s[1] == "checkout"]
                lats_b = [s[2] for s in rows if s[1] == "browse"]
                errs = sum(1 for s in rows if not s[3])
                test.seconds.append(
                    {
                        "t": round(t_offset + sec, 1),
                        "offered_rps": rps,
                        "completed": len(rows),
                        "checkout_p95_ms": round(_pct(lats_c, 95), 1),
                        "checkout_p50_ms": round(_pct(lats_c, 50), 1),
                        "browse_p95_ms": round(_pct(lats_b, 95), 1),
                        "error_pct": round(100 * errs / len(rows), 2) if rows else 0.0,
                        "inflight": len(inflight),
                    }
                )
                bucket_next += 1

        while not test.cancel:
            elapsed = time.perf_counter() - start
            if elapsed >= duration:
                break
            due = int(elapsed / interval) + 1
            while sent < due:
                if len(inflight) >= self.MAX_INFLIGHT:
                    dropped += 1
                else:
                    kind = "checkout" if random.random() < checkout_share else "browse"
                    task = asyncio.create_task(fire(kind))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                sent += 1
            flush(int(elapsed) - 1)
            await asyncio.sleep(min(interval, 0.02))
        send_window = time.perf_counter() - start
        sent_rate = (sent - dropped) / send_window if send_window > 0 else 0.0
        if inflight:
            await asyncio.wait(inflight, timeout=10)
        unfinished = len(inflight)
        flush(int(math.ceil(duration)))

        lats_c = [s[2] for s in samples if s[1] == "checkout"]
        lats_b = [s[2] for s in samples if s[1] == "browse"]
        # Requests still unfinished after the drain timeout count as failures.
        errors = sum(1 for s in samples if not s[3]) + unfinished
        total = len(samples) + unfinished
        completed_in_window = sum(1 for s in samples if s[0] < duration)
        achieved = completed_in_window / send_window if send_window > 0 else 0.0
        return {
            "offered_rps": rps,
            "duration_s": round(send_window, 1),
            "requests": total,
            "achieved_rps": round(achieved, 1),
            "sent_rps": round(sent_rate, 1),
            # Load-generator validity check: did the client actually send what was asked?
            "client_saturated": sent_rate < 0.95 * rps or dropped > 0,
            "server_throughput_below_offered": achieved < 0.95 * rps,
            "unfinished_after_drain": unfinished,
            "dropped_by_client": dropped,
            "checkout_requests": len(lats_c),
            "checkout_p50_ms": round(_pct(lats_c, 50), 1),
            "checkout_p95_ms": round(_pct(lats_c, 95), 1),
            "checkout_p99_ms": round(_pct(lats_c, 99), 1),
            "browse_p95_ms": round(_pct(lats_b, 95), 1),
            "error_pct": round(100 * errors / total, 2) if total else 0.0,
        }

    async def run(self, test: LoadTest, testbed_config: dict[str, Any], slo: SLO) -> None:
        self.current = test
        test.status = "running"
        test.started_at = time.time()
        p = test.params
        share = float(p.get("checkout_share", 0.5))
        limits = httpx.Limits(max_connections=self.MAX_INFLIGHT, max_keepalive_connections=100)
        try:
            async with httpx.AsyncClient(timeout=10.0, limits=limits) as client:
                if test.kind == "steady":
                    step = await self._phase(
                        test, client, float(p["rps"]), float(p["duration"]), share, 0
                    )
                    test.steps.append(step)
                elif test.kind == "flash":
                    base, peak = float(p["base_rps"]), float(p["peak_rps"])
                    t = 0.0
                    for rps, dur, label in (
                        (base, 5, "before"),
                        (peak, float(p["duration"]), "spike"),
                        (base, 8, "after"),
                    ):
                        if test.cancel:
                            break
                        step = await self._phase(test, client, rps, dur, share, t)
                        step["phase"] = label
                        test.steps.append(step)
                        t += dur
                else:  # sweep
                    t = 0.0
                    for rps in p["levels"]:
                        if test.cancel:
                            break
                        step = await self._phase(
                            test, client, float(rps), float(p["step_seconds"]), share, t
                        )
                        step["meets_slo"] = (
                            step["checkout_p95_ms"] <= slo.p95_ms
                            and step["error_pct"] <= slo.error_pct
                        )
                        test.steps.append(step)
                        t += float(p["step_seconds"])
                        # Abort condition: stop escalating once the system is clearly failing.
                        if step["error_pct"] > 50 or step["checkout_p95_ms"] > 10 * slo.p95_ms:
                            test.result["aborted"] = (
                                f"stopped after {rps} rps: error {step['error_pct']}%, "
                                f"p95 {step['checkout_p95_ms']} ms"
                            )
                            break
            test.result.update(self._summarise(test, testbed_config, slo))
            test.status = "cancelled" if test.cancel else "done"
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            test.status = "error"
            test.error = f"{type(exc).__name__}: {exc}"
        finally:
            test.finished_at = time.time()
            self.history.appendleft(
                {
                    "id": test.id,
                    "kind": test.kind,
                    "status": test.status,
                    "config": testbed_config,
                    "result": test.result,
                    "finished_at": test.finished_at,
                }
            )

    def _summarise(self, test: LoadTest, cfg: dict[str, Any], slo: SLO) -> dict[str, Any]:
        steps = test.steps
        out: dict[str, Any] = {"testbed_config": cfg, "slo": slo.__dict__}
        if not steps:
            return out
        if test.kind == "sweep":
            passing = [s for s in steps if s.get("meets_slo")]
            failing = [s for s in steps if not s.get("meets_slo")]
            last_ok = max((s["offered_rps"] for s in passing), default=None)
            first_bad = min(
                (s["offered_rps"] for s in failing if s["offered_rps"] > (last_ok or 0)),
                default=None,
            )
            out["observed_bound"] = {
                "last_rps_meeting_slo": last_ok,
                "first_rps_violating_slo": first_bad,
                "constraint": (
                    f"offered_rps > {last_ok:g} => SLO violation (bound in ({last_ok:g}, {first_bad:g}])"
                    if last_ok is not None and first_bad is not None
                    else (
                        "no violation observed up to "
                        f"{max(s['offered_rps'] for s in steps):g} rps (disappeared within tested region)"
                        if first_bad is None
                        else f"SLO violated at every tested level (bound below {min(s['offered_rps'] for s in steps):g} rps)"
                    )
                ),
                "tested_region": {
                    "rps": [
                        min(s["offered_rps"] for s in steps),
                        max(s["offered_rps"] for s in steps),
                    ],
                    **cfg,
                },
                "client_saturated_steps": [
                    s["offered_rps"] for s in steps if s["client_saturated"]
                ],
            }
        elif test.kind == "flash":
            out["phases"] = {s.get("phase", str(i)): s for i, s in enumerate(steps)}
            before = next((s for s in steps if s.get("phase") == "before"), None)
            after = next((s for s in steps if s.get("phase") == "after"), None)
            if before and after:
                out["recovered"] = after["checkout_p95_ms"] <= max(
                    slo.p95_ms, 1.5 * before["checkout_p95_ms"]
                ) and after["error_pct"] <= max(slo.error_pct, before["error_pct"] + 1)
        else:
            s = steps[0]
            out["meets_slo"] = (
                s["checkout_p95_ms"] <= slo.p95_ms and s["error_pct"] <= slo.error_pct
            )
        return out


# ---------------------------------------------------------------------------
# Store telemetry (the storefront's own upstream calls)
# ---------------------------------------------------------------------------


class StoreTelemetry:
    """Rolling window of real latencies the storefront observes calling the services."""

    def __init__(self, window_seconds: float = 30.0) -> None:
        self.window = window_seconds
        self.samples: deque[tuple[float, str, float, bool]] = deque(maxlen=5000)

    def record(self, route: str, latency_ms: float, ok: bool) -> None:
        self.samples.append((time.time(), route, latency_ms, ok))

    def snapshot(self, slo: SLO, load: LoadTest | None = None) -> dict[str, Any]:
        cutoff = time.time() - self.window
        recent = [s for s in self.samples if s[0] >= cutoff]
        lats = [s[2] for s in recent]
        errors = sum(1 for s in recent if not s[3])
        p95 = _pct(lats, 95)
        err = 100 * errors / len(recent) if recent else 0.0
        load_p95 = None
        if load is not None and load.status == "running" and load.seconds:
            last = load.seconds[-3:]
            load_p95 = max(s["checkout_p95_ms"] for s in last)
            err = max(err, max(s["error_pct"] for s in last))
        worst = max(p95, load_p95 or 0.0)
        if worst > slo.p95_ms or err > slo.error_pct:
            level = "degraded"
        elif worst > 0.6 * slo.p95_ms:
            level = "busy"
        else:
            level = "healthy"
        return {
            "level": level,
            "store_p95_ms": round(p95, 1),
            "load_test_p95_ms": load_p95,
            "error_pct": round(err, 2),
            "requests_in_window": len(recent),
            "window_seconds": self.window,
        }
