"""Builds a read-only static snapshot of the SCOPE-X Lab dashboard for static hosting (Vercel).

The live lab cannot run on serverless hosting: it launches six service processes and runs
multi-minute load tests. This script captures the current results from a running local
server (``python -m storefront``) into ``/snapshot/*.json`` and puts the dashboard in
read-only mode, so the deployed page shows exactly what the local testbed recorded.

Usage:
    python scripts/build_vercel_demo.py [--base http://127.0.0.1:8010] [--out build/scope-x-lab]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "storefront" / "static"
# Must match the default config object (and key order) in app.js modelTab().
WHATIF_DEFAULT = {
    "replicas": 4, "cpu_cores": 0.5, "memory_mib": 1024, "db_pool_size": 50,
    "cache_enabled": True, "target_rps": 300, "workload": "flash_sale", "mode": "static",
}


def _get(base: str, path: str, body: dict | None = None) -> object:
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _key(path: str) -> str:
    """Same mapping as snapKey() in app.js."""
    out = path.removeprefix("/api/")
    for ch in "/?=&":
        out = out.replace(ch, "_")
    return out + ".json"


def build(base: str, out: Path) -> dict[str, int]:
    if out.exists():
        shutil.rmtree(out)
    snap = out / "snapshot"
    snap.mkdir(parents=True)
    shutil.copytree(STATIC, out / "static", ignore=shutil.ignore_patterns("shield.js"))

    paths = ["/api/health", "/api/testbed", "/api/lab/history", "/api/lab/constraints", "/api/lab/shield"]
    history = _get(base, "/api/lab/history")
    paths += [f"/api/lab/tests/{h['id']}" for h in history]  # type: ignore[index]
    for path in paths:
        (snap / _key(path)).write_text(json.dumps(_get(base, path)))
    (snap / "lab_whatif.json").write_text(json.dumps(_get(base, "/api/lab/whatif", WHATIF_DEFAULT)))

    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    demo = {
        "captured": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "commit": commit,
        "whatif_default": WHATIF_DEFAULT,
    }
    html = (STATIC / "index.html").read_text()
    html = html.replace(
        '<script src="/static/charts.js"></script>',
        f"<script>window.SCOPEX_DEMO = {json.dumps(demo)};</script>\n  "
        '<script src="/static/charts.js"></script>',
    )
    (out / "index.html").write_text(html)
    (out / "vercel.json").write_text(json.dumps({"cleanUrls": True}, indent=2))
    return {"snapshots": len(list(snap.iterdir())), "runs": len(history)}  # type: ignore[arg-type]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8010")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "scope-x-lab")
    args = ap.parse_args()
    print(build(args.base.rstrip("/"), args.out), "->", args.out)
