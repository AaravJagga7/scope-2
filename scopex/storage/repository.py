"""SQLite and Parquet persistence repository for SCOPE-X experiment runs."""

from __future__ import annotations

import json
import sqlite3
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from scopex.storage.config import ExperimentConfig

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "scopex_experiments.db"
DEFAULT_PARQUET_PATH = BASE_DIR / "data" / "experiments.parquet"


def get_current_git_commit() -> str:
    """Retrieves current git commit hash for provenance tracking.

    Falls back to ``SCOPEX_GIT_COMMIT`` for deployments built without a ``.git`` directory.
    """
    if os.getenv("SCOPEX_GIT_COMMIT"):
        return os.environ["SCOPEX_GIT_COMMIT"]
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            cwd=str(BASE_DIR),
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


@dataclass
class ExperimentRun:
    """Record of a single executed experimental trial."""

    experiment_id: str
    run_id: str
    split: str  # "train", "held_out", "test"
    status: str  # "COMPLETED", "FAILED"
    git_commit: str
    created_at: str
    started_at: str
    completed_at: str
    config: ExperimentConfig
    group_name: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    throughput_rps: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    error_rate_pct: float = 0.0
    db_connections: float = 0.0
    cpu_pct: float = 0.0
    scaling_mode: str = "static"
    scaling_events: list[dict[str, Any]] = field(default_factory=list)
    replica_time_series: list[dict[str, Any]] = field(default_factory=list)
    pod_lifecycle_events: list[dict[str, Any]] = field(default_factory=list)
    restart_window_seconds: float = 0.0
    vpa_recommendations: list[dict[str, Any]] = field(default_factory=list)

    def to_flat_dict(self) -> dict[str, Any]:
        """Flattens run record for tabular storage and DataFrame export."""
        cfg_dict = self.config.to_dict()
        mode = self.scaling_mode or cfg_dict.get("scaling_mode", "static")
        return {
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "group_name": self.group_name or self.config.tags.get("group", ""),
            "split": self.split,
            "status": self.status,
            "git_commit": self.git_commit,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            # Flattened Config Parameters
            "target_rps": cfg_dict["workload"]["rps"],
            "workload_profile": cfg_dict["workload"]["profile"],
            "duration_seconds": cfg_dict["workload"]["duration"],
            "checkout_replicas": cfg_dict["checkout_replicas"],
            "db_pool_size": cfg_dict["db_pool_size"],
            "cpu_limit": cfg_dict["cpu_limit"],
            "cache_enabled": int(cfg_dict["cache_enabled"]),
            "consumers": cfg_dict["consumers"],
            "seed": cfg_dict["seed"],
            # Key Metrics
            "throughput_rps": self.throughput_rps,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "p99_latency_ms": self.p99_latency_ms,
            "error_rate_pct": self.error_rate_pct,
            "db_connections": self.db_connections,
            "cpu_pct": self.cpu_pct,
            # Autoscaling & Telemetry
            "scaling_mode": mode,
            "scaling_events_json": json.dumps(self.scaling_events),
            "replica_time_series_json": json.dumps(self.replica_time_series),
            "pod_lifecycle_json": json.dumps(self.pod_lifecycle_events),
            "restart_window_seconds": self.restart_window_seconds,
            "vpa_recommendations_json": json.dumps(self.vpa_recommendations),
            # Full JSON serializations
            "config_json": json.dumps(cfg_dict),
            "metrics_json": json.dumps(self.metrics),
        }


class ExperimentRepository:
    """Manages dual persistence in SQLite and Parquet for experiment reproducibility."""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        parquet_path: Path | str = DEFAULT_PARQUET_PATH,
    ) -> None:
        self.db_path = Path(db_path)
        self.parquet_path = Path(parquet_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()

    def _init_sqlite(self) -> None:
        """Initializes the SQLite schema if not already present."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS experiment_runs (
                    run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    group_name TEXT DEFAULT '',
                    split TEXT NOT NULL,
                    status TEXT NOT NULL,
                    git_commit TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    target_rps INTEGER,
                    workload_profile TEXT,
                    duration_seconds INTEGER,
                    checkout_replicas INTEGER,
                    db_pool_size INTEGER,
                    cpu_limit TEXT,
                    cache_enabled INTEGER,
                    consumers INTEGER,
                    seed INTEGER,
                    throughput_rps REAL,
                    p50_latency_ms REAL,
                    p95_latency_ms REAL,
                    p99_latency_ms REAL,
                    error_rate_pct REAL,
                    db_connections REAL,
                    cpu_pct REAL,
                    config_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL
                )
            """)
            try:
                conn.execute("ALTER TABLE experiment_runs ADD COLUMN group_name TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already exists
            for col_def in [
                "scaling_mode TEXT DEFAULT 'static'",
                "scaling_events_json TEXT DEFAULT '[]'",
                "replica_time_series_json TEXT DEFAULT '[]'",
                "pod_lifecycle_json TEXT DEFAULT '[]'",
                "restart_window_seconds REAL DEFAULT 0.0",
                "vpa_recommendations_json TEXT DEFAULT '[]'",
            ]:
                try:
                    conn.execute(f"ALTER TABLE experiment_runs ADD COLUMN {col_def}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_id ON experiment_runs(experiment_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_split ON experiment_runs(split)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_group ON experiment_runs(group_name)")
            conn.commit()

    def save_run(self, run: ExperimentRun) -> None:
        """Persists an experiment trial to SQLite and updates the Parquet archive."""
        row = run.to_flat_dict()
        columns = list(row.keys())
        placeholders = ", ".join(["?"] * len(columns))
        col_names = ", ".join(columns)
        values = [row[c] for c in columns]

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO experiment_runs ({col_names}) VALUES ({placeholders})",
                values,
            )
            conn.commit()

        # Update Parquet export
        self.export_to_parquet()

    def save_runs_batch(self, runs: list[ExperimentRun]) -> None:
        """Persists multiple experiment trials in a single SQLite transaction and exports to Parquet."""
        if not runs:
            return

        with sqlite3.connect(self.db_path) as conn:
            for run in runs:
                row = run.to_flat_dict()
                columns = list(row.keys())
                placeholders = ", ".join(["?"] * len(columns))
                col_names = ", ".join(columns)
                values = [row[c] for c in columns]
                conn.execute(
                    f"INSERT OR REPLACE INTO experiment_runs ({col_names}) VALUES ({placeholders})",
                    values,
                )
            conn.commit()

        self.export_to_parquet()

    def count_runs(self, split: str | None = None) -> int:
        """Returns total number of recorded runs, optionally filtered by split."""
        with sqlite3.connect(self.db_path) as conn:
            if split:
                cur = conn.execute("SELECT COUNT(*) FROM experiment_runs WHERE split = ?", (split,))
            else:
                cur = conn.execute("SELECT COUNT(*) FROM experiment_runs")
            return int(cur.fetchone()[0])

    def get_runs_by_experiment(self, experiment_id: str) -> list[ExperimentRun]:
        """Fetches all trials executed under a specific experiment_id."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM experiment_runs WHERE experiment_id = ? ORDER BY started_at ASC",
                (experiment_id,),
            )
            rows = cursor.fetchall()
            return [self._row_to_run(r) for r in rows]

    def list_runs(self, split: str | None = None, limit: int = 1000) -> list[ExperimentRun]:
        """Lists runs filtered by optional split tag ('train', 'held_out', 'test')."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if split:
                cursor = conn.execute(
                    "SELECT * FROM experiment_runs WHERE split = ? ORDER BY started_at DESC LIMIT ?",
                    (split, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM experiment_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                )
            rows = cursor.fetchall()
            return [self._row_to_run(r) for r in rows]

    def to_dataframe(self, split: str | None = None) -> pd.DataFrame:
        """Exports runs as a pandas DataFrame."""
        with sqlite3.connect(self.db_path) as conn:
            if split:
                df = pd.read_sql_query(
                    "SELECT * FROM experiment_runs WHERE split = ? ORDER BY started_at ASC",
                    conn,
                    params=(split,),
                )
            else:
                df = pd.read_sql_query(
                    "SELECT * FROM experiment_runs ORDER BY started_at ASC", conn
                )
        return df

    def export_to_parquet(self, target_path: Path | None = None) -> Path:
        """Exports the entire experiment repository to a Parquet file."""
        df = self.to_dataframe()
        out_path = Path(target_path or self.parquet_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if not df.empty:
            df.to_parquet(out_path, engine="pyarrow", index=False)
        return out_path

    def _row_to_run(self, row: sqlite3.Row) -> ExperimentRun:
        """Hydrates an SQLite row into an ExperimentRun instance."""
        cfg_dict = json.loads(row["config_json"])
        cfg = ExperimentConfig.from_dict(cfg_dict)
        metrics_dict = json.loads(row["metrics_json"])
        row_keys = set(row.keys())
        group = row["group_name"] if "group_name" in row_keys and row["group_name"] else ""
        scaling_mode = (
            row["scaling_mode"]
            if "scaling_mode" in row_keys and row["scaling_mode"]
            else cfg.scaling_mode
        )
        scaling_events = (
            json.loads(row["scaling_events_json"])
            if "scaling_events_json" in row_keys and row["scaling_events_json"]
            else []
        )
        replica_ts = (
            json.loads(row["replica_time_series_json"])
            if "replica_time_series_json" in row_keys and row["replica_time_series_json"]
            else []
        )
        pod_events = (
            json.loads(row["pod_lifecycle_json"])
            if "pod_lifecycle_json" in row_keys and row["pod_lifecycle_json"]
            else []
        )
        restart_window = (
            float(row["restart_window_seconds"])
            if "restart_window_seconds" in row_keys and row["restart_window_seconds"] is not None
            else 0.0
        )
        vpa_recs = (
            json.loads(row["vpa_recommendations_json"])
            if "vpa_recommendations_json" in row_keys and row["vpa_recommendations_json"]
            else []
        )
        return ExperimentRun(
            experiment_id=row["experiment_id"],
            run_id=row["run_id"],
            group_name=group,
            split=row["split"],
            status=row["status"],
            git_commit=row["git_commit"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            config=cfg,
            metrics=metrics_dict,
            throughput_rps=float(row["throughput_rps"]),
            p50_latency_ms=float(row["p50_latency_ms"]),
            p95_latency_ms=float(row["p95_latency_ms"]),
            p99_latency_ms=float(row["p99_latency_ms"]),
            error_rate_pct=float(row["error_rate_pct"]),
            db_connections=float(row["db_connections"]),
            cpu_pct=float(row["cpu_pct"]),
            scaling_mode=scaling_mode,
            scaling_events=scaling_events,
            replica_time_series=replica_ts,
            pod_lifecycle_events=pod_events,
            restart_window_seconds=restart_window,
            vpa_recommendations=vpa_recs,
        )
