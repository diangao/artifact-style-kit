"""Shared helpers for artifact-style-kit agent-facing scripts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_PATH = Path(".style-kit-state.json")
STATE_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2))


def ok_payload(data: dict[str, Any], recommended_next: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "status": "ok",
        "data": data,
        "recommended_next": recommended_next or [],
    }


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": STATE_VERSION,
            "current_run": None,
            "iteration": 0,
            "runs": {},
            "updated_at": now_iso(),
        }
    return json.loads(path.read_text())


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    state["schema_version"] = STATE_VERSION
    state["updated_at"] = now_iso()
    path.write_text(json.dumps(state, indent=2) + "\n")


def update_run_state(run_name: str, run_data: dict[str, Any], path: Path = STATE_PATH) -> dict[str, Any]:
    state = load_state(path)
    state["current_run"] = run_name
    state["iteration"] = max(int(state.get("iteration", 0)), int(run_data.get("iteration", 1)))
    runs = state.setdefault("runs", {})
    existing = runs.get(run_name, {})
    existing.update(run_data)
    existing["updated_at"] = now_iso()
    runs[run_name] = existing
    save_state(state, path)
    return state

