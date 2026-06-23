#!/usr/bin/env python3
"""Run a local visual UI for artifact-style-kit.

The UI is deliberately thin: it calls the existing CLI scripts, reads the same
.style-kit-state.json file, and displays the resulting run artifacts.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from build_contact_sheet import build_sheet, parse_color


REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = REPO_ROOT / ".style-kit-state.json"
RUNTIME_ORDER = ["codex", "claude", "cursor"]
SUPPORTED_RUNTIMES = {"codex", "claude"}


def nowish_error(message: str, status: int = 400) -> tuple[int, dict[str, Any]]:
    return status, {"status": "error", "error": message}


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-").lower()
    return slug or "style"


def repo_path(value: str | None) -> Path:
    if not value:
        raise ValueError("missing path")
    candidate = (REPO_ROOT / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    if candidate != REPO_ROOT and REPO_ROOT not in candidate.parents:
        raise ValueError("path is outside this repository")
    return candidate


def rel(path: str | Path | None) -> str | None:
    if not path:
        return None
    resolved = repo_path(str(path))
    return str(resolved.relative_to(REPO_ROOT))


def detect_runtimes() -> list[dict[str, Any]]:
    runtimes: list[dict[str, Any]] = []
    for name in RUNTIME_ORDER:
        binary = shutil.which(name)
        runtimes.append(
            {
                "name": name,
                "available": bool(binary),
                "path": binary,
                "supported": name in SUPPORTED_RUNTIMES,
                "recommended": name == "codex",
            }
        )
    return runtimes


def prepare_error_message(stdout: str, stderr: str) -> tuple[int, str]:
    raw = (stderr.strip() or stdout.strip() or "prepare_agent_run.py failed").strip()
    if "robots.txt disallows fetching" in raw:
        match = re.search(r"robots\.txt disallows fetching\s+(\S+)", raw)
        source = match.group(1) if match else "this source"
        return (
            409,
            f"{source} blocks automated asset fetching via robots.txt. Use a different source URL, "
            "or collect reference images manually and run the CLI with --reference-dir.",
        )
    if "no reference images found" in raw:
        return 422, "No reference images were found. Use a source with direct image assets or provide a reference folder."
    if "run name cannot be empty" in raw:
        return 422, "The target could not be converted into a run name. Add a run name or use a more specific target."
    compact = " ".join(line.strip() for line in raw.splitlines() if line.strip())
    compact = compact.replace("Error: ", "").replace("Next action: ", "")
    return 500, compact or "prepare_agent_run.py failed"


def image_files(value: str | None) -> list[dict[str, str]]:
    if not value:
        return []
    root = repo_path(value)
    if not root.exists():
        return []
    paths = sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
    )
    return [{"name": item.name, "path": rel(item)} for item in paths]


def process_is_running(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sync_artifact_state(
    state: dict[str, Any],
    current_run: str | None,
    run: dict[str, Any],
    artifacts: dict[str, list[dict[str, str]]],
    files: dict[str, Any],
) -> dict[str, Any]:
    if not current_run or not run:
        return run
    comparison = bool(files.get("comparison") and repo_path(files.get("comparison")).exists())
    has_generated = bool(artifacts.get("generated"))
    has_cutouts = bool(artifacts.get("cutouts"))
    updates: dict[str, Any] = {}
    if run.get("has_generated") != has_generated:
        updates["has_generated"] = has_generated
    if run.get("has_cutouts") != has_cutouts:
        updates["has_cutouts"] = has_cutouts
    if run.get("has_comparison") != comparison:
        updates["has_comparison"] = comparison
    terminal_statuses = {"locked", "runtime_failed"}
    if run.get("status") not in terminal_statuses and (has_cutouts or has_generated):
        updates["status"] = "candidate_ready"
        updates["recommended_next"] = {
            "command": "Review the candidate in the UI, then lock the style or loop again.",
            "why": "Generated files have landed in the run folder.",
        }
    if updates:
        run.update(updates)
        state.setdefault("runs", {})[current_run] = run
        save_json(STATE_PATH, state)
    return run


def default_source_review(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "draft",
        "subject": run.get("subject"),
        "source_url": run.get("source_url") or "manual reference folder",
        "review_goal": "Confirm that the extracted elements/contact sheet are the right source evidence.",
        "ignored_reference_assets": [],
        "missing_reference_notes": "",
        "style_notes": "",
    }


def ensure_source_review(state: dict[str, Any], current_run: str | None, run: dict[str, Any]) -> dict[str, Any]:
    if not current_run or not run:
        return run
    files = run.setdefault("files", {})
    if files.get("source_review") or not files.get("contact_sheet"):
        return run
    run_dir = repo_path(files.get("run_dir")) if files.get("run_dir") else repo_path("outputs/runs") / current_run
    source_review = run_dir / "source-review.json"
    if not source_review.exists():
        source_review.parent.mkdir(parents=True, exist_ok=True)
        save_json(source_review, default_source_review(run))
    files["source_review"] = rel(source_review)
    run["source_review_status"] = "draft"
    if not image_files(files.get("generated_dir")) and not image_files(files.get("cutouts_dir")):
        run["status"] = "prepared"
        run["recommended_next"] = {
            "command": f"Review {files['source_review']} and confirm/delete/supplement extracted elements before generation.",
            "why": "The run is prepared; the next step is validating the extracted source evidence.",
        }
    state.setdefault("runs", {})[current_run] = run
    save_json(STATE_PATH, state)
    return run


def run_next_action() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "scripts/next_action.py", "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "status": "error",
            "error": proc.stderr.strip() or proc.stdout.strip() or "next_action.py failed",
        }
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": proc.stdout.strip()}


def current_view() -> dict[str, Any]:
    state = load_json(STATE_PATH)
    current_run = state.get("current_run")
    run = state.get("runs", {}).get(current_run, {}) if current_run else {}
    run = ensure_source_review(state, current_run, run)
    files = run.get("files", {})
    artifacts = {
        "reference_assets": image_files(files.get("reference_dir")),
        "generated": image_files(files.get("generated_dir")),
        "cutouts": image_files(files.get("cutouts_dir")),
    }
    run = sync_artifact_state(state, current_run, run, artifacts, files)
    key_files = {
        key: rel(files.get(key))
        for key in [
            "contact_sheet",
            "review_contact_sheet",
            "comparison",
            "source_review",
            "prompt",
            "taste_notes",
            "agent_brief",
        ]
        if files.get(key) and repo_path(files.get(key)).exists()
    }
    source_review = {}
    if files.get("source_review"):
        source_review_path = repo_path(files.get("source_review"))
        if source_review_path.exists():
            try:
                source_review = json.loads(source_review_path.read_text())
            except json.JSONDecodeError:
                source_review = {"status": "invalid", "error": "source-review.json is not valid JSON"}
    return {
        "status": "ok",
        "repo": str(REPO_ROOT),
        "runtimes": detect_runtimes(),
        "state": state,
        "current_run": current_run,
        "run": run,
        "source_review": source_review,
        "files": key_files,
        "artifacts": artifacts,
        "locked_styles": state.get("locked_styles", []),
        "runtime_active": process_is_running(run.get("runtime_pid")),
        "next_action": run_next_action(),
    }


def prepare_run(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    subject = str(payload.get("subject", "")).strip()
    source_url = str(payload.get("source_url", "")).strip()
    run_name = str(payload.get("run_name", "")).strip()
    include = [str(item).strip() for item in payload.get("include", []) if str(item).strip()]
    exclude = [str(item).strip() for item in payload.get("exclude", []) if str(item).strip()]
    try:
        max_iterations = int(payload.get("max_iterations") or 5)
    except (TypeError, ValueError):
        return nowish_error("max_iterations must be a number")

    if not subject:
        return nowish_error("target is required")
    if not source_url:
        return nowish_error("source_url is required")
    if max_iterations < 1 or max_iterations > 20:
        return nowish_error("max_iterations must be between 1 and 20")

    cmd = [
        sys.executable,
        "scripts/prepare_agent_run.py",
        "--subject",
        subject,
        "--source-url",
        source_url,
        "--max-iterations",
        str(max_iterations),
    ]
    if run_name:
        cmd.extend(["--run-name", run_name])
    for pattern in include:
        cmd.extend(["--include", pattern])
    for pattern in exclude:
        cmd.extend(["--exclude", pattern])

    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        status, message = prepare_error_message(proc.stdout, proc.stderr)
        return status, {
            "status": "error",
            "error": message,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {"status": "ok", "raw": proc.stdout}
    return 200, {"status": "ok", "prepare": data, "view": current_view()}


def lock_style(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    candidate = str(payload.get("candidate_path", "")).strip()
    if not candidate:
        return nowish_error("candidate_path is required")
    candidate_path = repo_path(candidate)
    if not candidate_path.exists():
        return nowish_error("candidate_path does not exist", 404)
    style_name = str(payload.get("style_name", "")).strip()

    state = load_json(STATE_PATH)
    current_run = state.get("current_run")
    run = state.get("runs", {}).get(current_run, {}) if current_run else {}
    if not run:
        return nowish_error("no current run to lock")
    files = run.get("files", {})
    if not style_name:
        style_name = str(run.get("subject") or current_run or "Untitled style")
    existing_ids = {item.get("id") for item in state.get("locked_styles", []) if isinstance(item, dict)}
    base_id = slugify(style_name)
    style_id = base_id
    suffix = 2
    while style_id in existing_ids:
        style_id = f"{base_id}-{suffix}"
        suffix += 1
    locked = {
        "id": style_id,
        "name": style_name,
        "source_url": run.get("source_url"),
        "subject": run.get("subject"),
        "run_name": current_run,
        "reference_dir": rel(files.get("reference_dir")) if files.get("reference_dir") else None,
        "contact_sheet": rel(files.get("contact_sheet")),
        "review_contact_sheet": rel(files.get("review_contact_sheet")) if files.get("review_contact_sheet") else None,
        "source_review": rel(files.get("source_review")) if files.get("source_review") else None,
        "accepted_candidate": rel(candidate_path),
        "prompt": rel(files.get("prompt")),
        "taste_notes": rel(files.get("taste_notes")),
        "reference_manifest": rel(files.get("assets_manifest")) if files.get("assets_manifest") else None,
        "key_color": str(payload.get("key_color") or "ff00ff"),
        "locked_by": "stylekit_ui",
        "created_at": utc_now(),
    }
    state["locked_style"] = locked
    styles = [item for item in state.get("locked_styles", []) if isinstance(item, dict)]
    styles = [item for item in styles if item.get("id") != style_id]
    styles.append(locked)
    state["locked_styles"] = styles
    state.setdefault("runs", {}).setdefault(current_run, {}).update({"status": "locked", "accepted_candidate": rel(candidate_path)})
    save_json(STATE_PATH, state)
    return 200, {"status": "ok", "locked_style": locked, "view": current_view()}


def prepare_from_style(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    style_id = str(payload.get("style_id", "")).strip()
    subject = str(payload.get("subject", "")).strip()
    if not style_id:
        return nowish_error("style_id is required")
    if not subject:
        return nowish_error("target is required")

    state = load_json(STATE_PATH)
    styles = [item for item in state.get("locked_styles", []) if isinstance(item, dict)]
    style = next((item for item in styles if item.get("id") == style_id), None)
    if not style:
        return nowish_error("style not found", 404)
    reference_dir = style.get("reference_dir")
    if not reference_dir:
        return nowish_error("locked style has no reference_dir", 422)

    try:
        max_iterations = int(payload.get("max_iterations") or 5)
    except (TypeError, ValueError):
        return nowish_error("max_iterations must be a number")

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/prepare_agent_run.py",
            "--subject",
            subject,
            "--reference-dir",
            str(repo_path(reference_dir)),
            "--max-iterations",
            str(max_iterations),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        status, message = prepare_error_message(proc.stdout, proc.stderr)
        return status, {"status": "error", "error": message, "stdout": proc.stdout, "stderr": proc.stderr}

    state = load_json(STATE_PATH)
    current_run = state.get("current_run")
    run = state.get("runs", {}).get(current_run, {}) if current_run else {}
    files = run.get("files", {})
    review = load_json(repo_path(style.get("source_review"))) if style.get("source_review") else default_source_review(run)
    ignored = [str(item) for item in review.get("ignored_reference_assets", []) if str(item)]
    review.update(
        {
            "status": "confirmed",
            "subject": subject,
            "source_url": f"locked style: {style.get('name')}",
            "review_goal": f"Reuse locked style '{style.get('name')}' for the new target.",
            "ignored_reference_assets": ignored,
        }
    )
    review_path = repo_path(files.get("source_review"))
    save_json(review_path, review)
    review_contact_sheet = write_review_contact_sheet(files, ignored)
    if review_contact_sheet:
        files["review_contact_sheet"] = review_contact_sheet
    state.setdefault("runs", {}).setdefault(current_run, {}).update(
        {
            "source_review_status": "confirmed",
            "status": "source_reviewed",
            "using_locked_style": style.get("id"),
            "source_url": f"locked style: {style.get('name')}",
            "files": files,
            "recommended_next": {
                "command": f"Read {files.get('agent_brief')} and generate candidates from {files.get('prompt')}.",
                "why": "A locked style is selected; generate the new target from that style evidence.",
            },
        }
    )
    save_json(STATE_PATH, state)
    return 200, {"status": "ok", "style": style, "view": current_view()}


def start_runtime(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    runtime = str(payload.get("runtime") or "codex").strip()
    runtime_info = next((item for item in detect_runtimes() if item["name"] == runtime), None)
    if not runtime_info:
        return nowish_error(f"unknown runtime: {runtime}")
    if not runtime_info["supported"]:
        return nowish_error(f"{runtime} is detected in the UI list but is not wired to the launcher yet")
    if not runtime_info["available"]:
        return nowish_error(f"{runtime} executable was not found on PATH", 404)

    state = load_json(STATE_PATH)
    current_run = state.get("current_run")
    run = state.get("runs", {}).get(current_run, {}) if current_run else {}
    if not current_run or not run:
        return nowish_error("no current run to generate", 404)
    if run.get("source_review_status") != "confirmed":
        return nowish_error("confirm source elements before starting generation")
    if run.get("status") == "generating":
        return nowish_error("generation is already running")
    if process_is_running(run.get("runtime_pid")):
        return nowish_error("runtime is still finishing; refresh in a moment before looping again", 409)

    files = run.get("files", {})
    log_path = repo_path(files.get("run_dir")) / "agent-runtime.launch.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state.setdefault("runs", {}).setdefault(current_run, {}).update(
        {
            "status": "generating",
            "runtime": runtime,
            "runtime_launch_log": rel(log_path),
        }
    )
    save_json(STATE_PATH, state)

    with log_path.open("a") as log:
        subprocess.Popen(
            [sys.executable, "scripts/run_agent_runtime.py", "--run", current_run, "--runtime", runtime],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return 200, {"status": "ok", "runtime": runtime, "view": current_view()}


def write_review_contact_sheet(files: dict[str, Any], ignored_reference_assets: list[str]) -> str | None:
    reference_dir = repo_path(files.get("reference_dir"))
    assets = image_files(files.get("reference_dir"))
    ignored = set(ignored_reference_assets)
    kept = [repo_path(item["path"]) for item in assets if item["path"] not in ignored]
    output = repo_path(files.get("run_dir")) / "review-contact-sheet.jpg"
    if not kept:
        if output.exists():
            output.unlink()
        return None
    build_sheet(
        paths=kept,
        output=output,
        cell_size=160,
        columns=8,
        label_height=24,
        background=parse_color("e8e9e0"),
        labels=True,
        base_dir=reference_dir,
    )
    return rel(output)


def save_source_review(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    state = load_json(STATE_PATH)
    current_run = state.get("current_run")
    run = state.get("runs", {}).get(current_run, {}) if current_run else {}
    files = run.get("files", {})
    if not run or not files.get("source_review"):
        return nowish_error("no current source review", 404)

    ignored_reference_assets = [
        str(item).strip() for item in payload.get("ignored_reference_assets", []) if str(item).strip()
    ]
    missing_reference_notes = str(payload.get("missing_reference_notes", "")).strip()
    style_notes = str(payload.get("style_notes", "")).strip()
    review_goal = (
        str(payload.get("review_goal", "")).strip()
        or "Confirm that the extracted elements/contact sheet are the right source evidence."
    )

    review_path = repo_path(files.get("source_review"))
    review = load_json(review_path) if review_path.exists() else {}
    review_contact_sheet = write_review_contact_sheet(files, ignored_reference_assets)
    if review_contact_sheet:
        files["review_contact_sheet"] = review_contact_sheet
    else:
        files.pop("review_contact_sheet", None)
    review.update(
        {
            "status": "confirmed",
            "subject": run.get("subject"),
            "source_url": run.get("source_url") or "manual reference folder",
            "review_goal": review_goal,
            "ignored_reference_assets": ignored_reference_assets,
            "missing_reference_notes": missing_reference_notes,
            "style_notes": style_notes,
        }
    )
    save_json(review_path, review)
    state.setdefault("runs", {}).setdefault(current_run, {}).update(
        {
            "source_review_status": "confirmed",
            "status": "source_reviewed",
            "recommended_next": {
                "command": f"Read {files.get('agent_brief')} and generate candidates from {files.get('prompt')}.",
                "why": "The source extraction is confirmed and no generated candidates are present yet.",
            },
            "files": files,
        }
    )
    save_json(STATE_PATH, state)
    return 200, {"status": "ok", "source_review": review, "view": current_view()}


def text_payload(path_value: str | None) -> tuple[int, dict[str, Any]]:
    try:
        path = repo_path(path_value)
    except ValueError as exc:
        return nowish_error(str(exc))
    if not path.exists():
        return nowish_error("file does not exist", 404)
    return 200, {"status": "ok", "path": rel(path), "text": path.read_text(errors="replace")}


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>artifact-style-kit</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #171f1c;
      --muted: #69736f;
      --line: #d9ded8;
      --paper: #f7f7f2;
      --panel: #ffffff;
      --soft: #eef1ec;
      --ok: #226c4b;
      --bad: #a43434;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body { margin: 0; min-height: 100vh; background: var(--paper); color: var(--ink); }
    button, input, textarea { font: inherit; }
    .app { width: min(920px, calc(100vw - 32px)); margin: 0 auto; padding: 38px 0 56px; }
    header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 34px; }
    h1 { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: 0; }
    .step-count { color: var(--muted); font-size: 13px; }
    .screen { display: none; }
    .screen.active { display: block; }
    .prompt { max-width: 720px; margin: 0 auto; display: grid; gap: 18px; }
    .prompt h2 { margin: 0; font-size: clamp(34px, 6vw, 68px); line-height: 1.02; font-weight: 620; letter-spacing: 0; }
    .field { display: grid; gap: 10px; }
    label { color: var(--muted); font-size: 13px; }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      color: var(--ink);
      padding: 16px 17px;
      font-size: 18px;
      outline: none;
      box-shadow: 0 1px 0 rgba(0,0,0,.02);
    }
    textarea { min-height: 112px; resize: vertical; }
    input:focus, textarea:focus { border-color: #aeb8b2; box-shadow: 0 0 0 3px rgba(23,31,28,.06); }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    button {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      color: var(--ink);
      min-height: 44px;
      padding: 0 16px;
      cursor: pointer;
    }
    button.primary { background: var(--ink); border-color: var(--ink); color: white; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    select {
      min-height: 44px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      color: var(--ink);
      padding: 0 34px 0 12px;
      font: inherit;
    }
    .runtime-picker {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    .style-name {
      width: 190px;
      min-height: 44px;
      padding: 0 12px;
      font-size: 15px;
    }
    .message { min-height: 22px; color: var(--muted); font-size: 13px; }
    .message.error { color: var(--bad); }
    .style-library {
      display: grid;
      gap: 10px;
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }
    .style-library h3 { margin: 0; font-size: 13px; color: var(--muted); font-weight: 600; }
    .style-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
    .style-card {
      display: grid;
      grid-template-columns: 48px 1fr;
      gap: 10px;
      align-items: center;
      min-height: 70px;
      padding: 10px;
      text-align: left;
    }
    .style-card img { width: 48px; height: 48px; object-fit: contain; border-radius: 6px; background: var(--soft); }
    .style-card strong, .style-card span {
      display: block;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .style-card span { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .review { display: grid; gap: 18px; }
    .review-head { display: flex; justify-content: space-between; align-items: end; gap: 16px; flex-wrap: wrap; }
    .review-head h2 { margin: 0; font-size: 24px; line-height: 1.15; }
    .artifact-main {
      border: 1px solid var(--line);
      background: white;
      border-radius: 8px;
      min-height: 520px;
      display: grid;
      place-items: center;
      overflow: hidden;
    }
    .artifact-main img { width: 100%; max-height: 76vh; object-fit: contain; display: block; background: var(--soft); }
    .source-pass {
      width: 100%;
      min-height: 520px;
      display: grid;
      grid-template-columns: minmax(260px, .9fr) minmax(300px, 1.1fr);
      gap: 18px;
      align-items: stretch;
      padding: 18px;
    }
    .source-waiting {
      width: min(760px, 100%);
      display: grid;
      gap: 18px;
      padding: 18px;
      align-items: center;
    }
    .source-preview, .source-editor {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfbf7;
      min-width: 0;
      padding: 14px;
    }
    .source-preview img {
      width: 100%;
      max-height: 300px;
      object-fit: contain;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--soft);
      display: block;
      margin-bottom: 12px;
    }
    .source-editor { display: grid; gap: 14px; align-content: start; }
    .source-editor h3, .source-preview h3 { margin: 0 0 10px; font-size: 13px; color: var(--muted); font-weight: 600; }
    .asset-option input { width: auto; box-shadow: none; }
    .asset-strip {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(72px, 1fr));
      gap: 8px;
      max-height: 180px;
      overflow: auto;
    }
    .asset-option {
      position: relative;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: white;
      overflow: hidden;
      min-height: 72px;
    }
    .asset-option img { width: 100%; height: 72px; object-fit: cover; display: block; background: var(--soft); }
    .asset-option input { position: absolute; top: 6px; left: 6px; accent-color: var(--ink); }
    .asset-option.dropped img { opacity: .28; filter: grayscale(1); }
    .review-notes { min-height: 78px; font-size: 14px; padding: 10px 12px; }
    .empty {
      width: min(520px, 100%);
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 28px;
      color: var(--muted);
      line-height: 1.45;
      background: #fbfbf7;
      text-align: center;
    }
    .thumbs { display: flex; gap: 10px; flex-wrap: wrap; }
    .thumb {
      width: 96px;
      height: 76px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: white;
      padding: 0;
      overflow: hidden;
    }
    .thumb.selected { border-color: var(--ink); box-shadow: 0 0 0 2px var(--ink); }
    .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
    details {
      border-top: 1px solid var(--line);
      padding-top: 16px;
      color: var(--muted);
      font-size: 13px;
    }
    summary { cursor: pointer; color: var(--ink); margin-bottom: 12px; }
    .debug { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .debug-box { border: 1px solid var(--line); border-radius: 8px; background: white; padding: 12px; min-width: 0; }
    .debug-box h3 { margin: 0 0 8px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0; }
    pre {
      margin: 0;
      max-height: 280px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: var(--ink);
    }
    a { color: inherit; }
    @media (max-width: 700px) {
      .app { width: min(100vw - 24px, 920px); padding-top: 24px; }
      header { margin-bottom: 24px; }
      .artifact-main { min-height: 360px; }
      .source-pass { grid-template-columns: 1fr; padding: 12px; }
      .prompt h2 { font-size: 38px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <h1>artifact-style-kit</h1>
      <div id="stepCount" class="step-count">Step 1 of 2</div>
    </header>

    <section id="screenSource" class="screen active">
      <div class="prompt">
        <h2>Paste the style source.</h2>
        <div class="field">
          <label for="sourceUrl">Source URL</label>
          <input id="sourceUrl" placeholder="https://example.com/style-source" autocomplete="off" autofocus>
        </div>
        <div id="styleLibrary" class="style-library" hidden></div>
        <div class="actions">
          <button id="toTarget" class="primary">Next</button>
        </div>
        <div id="messageSource" class="message"></div>
      </div>
    </section>

    <section id="screenTarget" class="screen">
      <div class="prompt">
        <h2>Describe what to make.</h2>
        <div class="field">
          <label for="subject">Target</label>
          <textarea id="subject" placeholder="one small object or image request"></textarea>
        </div>
        <div class="actions">
          <button id="backToSource">Back</button>
          <button id="prepare" class="primary">Start</button>
        </div>
        <div id="messageTarget" class="message"></div>
      </div>
    </section>

    <section id="screenReview" class="screen">
      <div class="review">
        <div class="review-head">
          <div>
            <div id="runMeta" class="step-count"></div>
            <h2 id="reviewTitle">Review the result.</h2>
          </div>
          <div class="actions">
            <button id="newRun">New run</button>
            <button id="refresh">Refresh</button>
            <button id="confirmSource" class="primary">Use these elements</button>
            <label id="runtimePicker" class="runtime-picker" for="runtimeSelect">
              Runtime
              <select id="runtimeSelect"></select>
            </label>
            <button id="startRuntime" class="primary">Start runtime</button>
            <button id="loopAgain">Loop again</button>
            <input id="styleName" class="style-name" placeholder="Style name">
            <button id="approve" class="primary">Lock style</button>
          </div>
        </div>
        <div id="artifactMain" class="artifact-main"></div>
        <div id="thumbs" class="thumbs"></div>
        <div id="messageReview" class="message"></div>

        <details>
          <summary>Debug details</summary>
          <div class="debug">
            <div class="debug-box">
              <h3>Next action</h3>
              <pre id="nextAction"></pre>
            </div>
            <div class="debug-box">
              <h3>Agent brief</h3>
              <pre id="agentBrief"></pre>
            </div>
            <div class="debug-box">
              <h3>Source review</h3>
              <pre id="sourceReview"></pre>
            </div>
            <div class="debug-box">
              <h3>Contact sheet</h3>
              <div id="contactSheet"></div>
            </div>
          </div>
        </details>
      </div>
    </section>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let view = null;
    let selectedCandidate = null;
    let pendingSource = '';
    let pendingStyleId = '';
    let runtimePollTimer = null;

    function fileUrl(path) {
      return `/api/file?path=${encodeURIComponent(path)}`;
    }

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: {'content-type': 'application/json'},
        ...options
      });
      const data = await res.json();
      if (!res.ok || data.status === 'error') throw new Error(data.error || 'request failed');
      return data;
    }

    function screen(name) {
      ['Source', 'Target', 'Review'].forEach((item) => {
        $(`screen${item}`).classList.toggle('active', item === name);
      });
      $('stepCount').textContent = name === 'Source' ? 'Step 1 of 2' : name === 'Target' ? 'Step 2 of 2' : 'Review';
    }

    function setMessage(id, text, isError = false) {
      $(id).textContent = text || '';
      $(id).classList.toggle('error', Boolean(isError));
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, (char) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
      }[char]));
    }

    function candidates() {
      return [...(view?.artifacts?.cutouts || []), ...(view?.artifacts?.generated || [])];
    }

    function lockedStyles() {
      return Array.isArray(view?.locked_styles) ? view.locked_styles : [];
    }

    function needsSourceReview() {
      return Boolean(view?.files?.source_review) && !candidates().length && view?.source_review?.status !== 'confirmed';
    }

    function isGenerating() {
      return view?.run?.status === 'generating';
    }

    function defaultRuntimeName() {
      const runtimes = view?.runtimes || [];
      const recommended = runtimes.find((item) => item.available && item.supported && item.recommended);
      const fallback = runtimes.find((item) => item.available && item.supported);
      return (recommended || fallback)?.name || '';
    }

    function renderRuntimeSelect(canStartRuntime) {
      const select = $('runtimeSelect');
      const runtimes = view?.runtimes || [];
      const previous = select.value;
      if (!runtimes.length) {
        select.innerHTML = '<option value="">No runtime detected</option>';
        select.value = '';
        select.disabled = true;
        return;
      }
      select.innerHTML = runtimes.map((item) => {
        const details = item.available
          ? item.supported
            ? 'detected'
            : 'detected, not wired'
          : 'not found';
        const disabled = !item.available || !item.supported ? 'disabled' : '';
        return `<option value="${escapeHtml(item.name)}" ${disabled}>${escapeHtml(item.name)} (${details})</option>`;
      }).join('');
      const supportedNames = new Set(runtimes.filter((item) => item.available && item.supported).map((item) => item.name));
      select.value = supportedNames.has(previous) ? previous : defaultRuntimeName();
      select.disabled = !canStartRuntime || !select.value;
    }

    function selectedArtifact() {
      const items = candidates();
      return selectedCandidate || items[0]?.path || null;
    }

    async function loadText(path) {
      if (!path) return 'No file yet.';
      const data = await api(`/api/text?path=${encodeURIComponent(path)}`);
      return data.text;
    }

    async function refresh() {
      view = await api('/api/state');
      const run = view.run || {};
      $('runMeta').textContent = view.current_run ? `${view.current_run} · ${run.status || 'prepared'}` : 'No run yet';
      selectedCandidate = selectedCandidate || candidates()[0]?.path || null;
      renderStyleLibrary();
      renderMainArtifact();
      renderThumbs();
      await renderDebug();
      syncRuntimePolling();
    }

    function renderStyleLibrary() {
      const styles = lockedStyles();
      const library = $('styleLibrary');
      if (!styles.length) {
        library.hidden = true;
        library.innerHTML = '';
        return;
      }
      library.hidden = false;
      library.innerHTML = `
        <h3>Saved styles</h3>
        <div class="style-grid">
          ${styles.map((style) => `
            <button class="style-card" data-style="${escapeHtml(style.id)}" title="${escapeHtml(style.name || style.id)}">
              ${style.accepted_candidate ? `<img src="${fileUrl(style.accepted_candidate)}" alt="">` : '<span></span>'}
              <span>
                <strong>${escapeHtml(style.name || style.id)}</strong>
                <span>${escapeHtml(style.subject || style.run_name || '')}</span>
              </span>
            </button>
          `).join('')}
        </div>
      `;
      document.querySelectorAll('[data-style]').forEach((button) => {
        button.onclick = () => {
          pendingStyleId = button.dataset.style || '';
          pendingSource = '';
          setMessage('messageSource', '');
          screen('Target');
          $('subject').focus();
        };
      });
    }

    function syncRuntimePolling() {
      if (isGenerating()) {
        if (!runtimePollTimer) {
          runtimePollTimer = window.setInterval(() => {
            refresh().catch((error) => setMessage('messageReview', error.message, true));
          }, 3000);
        }
        return;
      }
      if (runtimePollTimer) {
        window.clearInterval(runtimePollTimer);
        runtimePollTimer = null;
      }
    }

    function renderMainArtifact() {
      const run = view?.run || {};
      const path = selectedArtifact();
      const needsReview = needsSourceReview();
      const canStartRuntime = !path && !needsReview && view?.source_review?.status === 'confirmed' && !isGenerating() && Boolean(defaultRuntimeName());
      $('reviewTitle').textContent = path
        ? 'Review the result.'
        : needsReview
          ? 'Review the extracted elements.'
          : isGenerating()
            ? 'Generating candidate.'
            : 'Waiting for agent output.';
      $('approve').hidden = !path;
      $('approve').disabled = !path;
      $('loopAgain').hidden = !path;
      $('loopAgain').disabled = !path || Boolean(view?.runtime_active);
      $('styleName').hidden = !path;
      $('styleName').disabled = !path;
      if (path && $('styleName').dataset.run !== view.current_run) {
        $('styleName').value = run.subject || view.current_run || '';
        $('styleName').dataset.run = view.current_run || '';
      }
      if (!path) {
        $('styleName').dataset.run = '';
      }
      $('confirmSource').hidden = !needsReview;
      $('confirmSource').disabled = !needsReview;
      $('runtimePicker').hidden = !canStartRuntime && !isGenerating();
      renderRuntimeSelect(canStartRuntime);
      $('startRuntime').hidden = !canStartRuntime && !isGenerating();
      $('startRuntime').disabled = !canStartRuntime;
      $('startRuntime').textContent = isGenerating() ? 'Generating...' : 'Start runtime';
      if (needsReview) {
        renderSourceReview();
        return;
      }
      if (!path) {
        renderWaitingForGeneration();
        return;
      }
      $('artifactMain').innerHTML = `<a href="${fileUrl(path)}" target="_blank"><img src="${fileUrl(path)}" alt="${path}"></a>`;
    }

    function renderWaitingForGeneration() {
      const sheet = view.files?.review_contact_sheet || view.files?.contact_sheet;
      const text = isGenerating()
        ? 'Generating is in progress. This screen will show the candidate when files land in the run folder.'
        : 'Source elements are confirmed. No generator is running in this UI yet; start an agent with the brief, then refresh.';
      $('artifactMain').innerHTML = `
        <div class="source-waiting">
          ${sheet ? `
            <div class="source-preview">
              <h3>Reviewed contact sheet</h3>
              <a href="${fileUrl(sheet)}" target="_blank"><img src="${fileUrl(sheet)}" alt="reviewed contact sheet"></a>
            </div>
          ` : ''}
          <div class="empty">${text}</div>
        </div>
      `;
    }

    function renderSourceReview() {
      const review = view.source_review || {};
      const ignored = new Set(Array.isArray(review.ignored_reference_assets) ? review.ignored_reference_assets : []);
      const assets = view.artifacts?.reference_assets || [];
      const sheet = view.files?.contact_sheet;
      $('artifactMain').innerHTML = `
        <div class="source-pass">
          <div class="source-preview">
            <h3>Contact sheet</h3>
            ${sheet ? `<a href="${fileUrl(sheet)}" target="_blank"><img src="${fileUrl(sheet)}" alt="contact sheet"></a>` : '<div class="empty">No contact sheet yet.</div>'}
            <h3>Extracted elements</h3>
            <div class="asset-strip">
              ${assets.map((item) => `
                <label class="asset-option ${ignored.has(item.path) ? 'dropped' : ''}" title="${escapeHtml(item.name)}">
                  <input type="checkbox" data-asset="${escapeHtml(item.path)}" ${ignored.has(item.path) ? '' : 'checked'}>
                  <img src="${fileUrl(item.path)}" alt="${escapeHtml(item.name)}">
                </label>
              `).join('') || '<div class="empty">No reference assets.</div>'}
            </div>
          </div>
          <div class="source-editor">
            <div class="field">
              <label for="reviewGoal">Review goal</label>
              <textarea id="reviewGoal" class="review-notes">${escapeHtml(review.review_goal || '')}</textarea>
            </div>
            <div class="field">
              <label for="missingNotes">Missing / supplemental source notes</label>
              <textarea id="missingNotes" class="review-notes" placeholder="anything important the extractor missed">${escapeHtml(review.missing_reference_notes || '')}</textarea>
            </div>
            <div class="field">
              <label for="styleNotes">Style notes for the next contract</label>
              <textarea id="styleNotes" class="review-notes" placeholder="e.g. use these only for palette/material, not object taxonomy">${escapeHtml(review.style_notes || '')}</textarea>
            </div>
          </div>
        </div>
      `;
      wireSourceReviewControls();
    }

    function wireSourceReviewControls() {
      document.querySelectorAll('[data-asset]').forEach((input) => {
        input.onchange = () => input.closest('.asset-option').classList.toggle('dropped', !input.checked);
      });
    }

    function renderThumbs() {
      const items = candidates();
      if (!items.length) {
        $('thumbs').innerHTML = '';
        return;
      }
      $('thumbs').innerHTML = items.map((item) => `
        <button class="thumb ${item.path === selectedCandidate ? 'selected' : ''}" data-candidate="${item.path}" title="${item.name}">
          <img src="${fileUrl(item.path)}" alt="${item.name}">
        </button>
      `).join('');
      document.querySelectorAll('[data-candidate]').forEach((button) => {
        button.onclick = () => {
          selectedCandidate = button.dataset.candidate;
          renderMainArtifact();
          renderThumbs();
        };
      });
    }

    async function renderDebug() {
      const next = view.next_action?.recommended_next?.[0];
      $('nextAction').textContent = next ? `${next.command}\n\n${next.why}` : JSON.stringify(view.next_action || {}, null, 2);
      $('agentBrief').textContent = await loadText(view.files?.agent_brief);
      $('sourceReview').textContent = await loadText(view.files?.source_review);
      const sheet = view.files?.review_contact_sheet || view.files?.contact_sheet;
      $('contactSheet').innerHTML = sheet
        ? `<a href="${fileUrl(sheet)}" target="_blank"><img src="${fileUrl(sheet)}" style="width:100%;border-radius:6px;border:1px solid var(--line);"></a>`
        : 'No contact sheet yet.';
    }

    async function prepare() {
      const source = pendingSource.trim();
      const subject = $('subject').value.trim();
      if (!subject) {
        setMessage('messageTarget', 'Add one target prompt.', true);
        return;
      }
      $('prepare').disabled = true;
      setMessage('messageTarget', 'Preparing...');
      try {
        const usingStyle = Boolean(pendingStyleId);
        const endpoint = usingStyle ? '/api/prepare-from-style' : '/api/prepare';
        const body = usingStyle
          ? {style_id: pendingStyleId, subject, max_iterations: 5}
          : {source_url: source, subject, max_iterations: 5};
        await api(endpoint, {
          method: 'POST',
          body: JSON.stringify(body)
        });
        selectedCandidate = null;
        pendingStyleId = '';
        await refresh();
        screen('Review');
        setMessage('messageReview', usingStyle ? 'Locked style selected. Generate a candidate.' : 'Review the extracted elements before generation.');
      } catch (error) {
        setMessage('messageTarget', error.message, true);
      } finally {
        $('prepare').disabled = false;
      }
    }

    async function lock() {
      if (!selectedCandidate) return;
      $('approve').disabled = true;
      try {
        await api('/api/lock', {
          method: 'POST',
          body: JSON.stringify({
            candidate_path: selectedCandidate,
            style_name: $('styleName').value.trim()
          })
        });
        await refresh();
        setMessage('messageReview', 'Style locked.');
      } catch (error) {
        setMessage('messageReview', error.message, true);
      } finally {
        $('approve').disabled = false;
      }
    }

    async function loopAgain() {
      $('loopAgain').disabled = true;
      const runtime = $('runtimeSelect').value || defaultRuntimeName();
      setMessage('messageReview', `Starting another ${runtime} pass...`);
      try {
        await api('/api/generate', {
          method: 'POST',
          body: JSON.stringify({runtime})
        });
        await refresh();
        setMessage('messageReview', 'Refinement started. The current candidate stays visible until new files replace it.');
      } catch (error) {
        setMessage('messageReview', error.message, true);
      } finally {
        renderMainArtifact();
      }
    }

    async function confirmSource() {
      const ignored = [...document.querySelectorAll('[data-asset]')]
        .filter((input) => !input.checked)
        .map((input) => input.dataset.asset);
      $('confirmSource').disabled = true;
      try {
        await api('/api/source-review', {
          method: 'POST',
          body: JSON.stringify({
            review_goal: $('reviewGoal')?.value || '',
            ignored_reference_assets: ignored,
            missing_reference_notes: $('missingNotes')?.value || '',
            style_notes: $('styleNotes')?.value || ''
          })
        });
        await refresh();
        setMessage('messageReview', 'Source elements confirmed. Generate a candidate from the agent brief.');
      } catch (error) {
        setMessage('messageReview', error.message, true);
      } finally {
        $('confirmSource').disabled = false;
      }
    }

    async function startRuntime() {
      $('startRuntime').disabled = true;
      const runtime = $('runtimeSelect').value || defaultRuntimeName();
      setMessage('messageReview', `Starting ${runtime} runtime...`);
      try {
        await api('/api/generate', {
          method: 'POST',
          body: JSON.stringify({runtime})
        });
        await refresh();
        setMessage('messageReview', 'Generation started. This page will update when artifacts land.');
      } catch (error) {
        setMessage('messageReview', error.message, true);
      } finally {
        renderMainArtifact();
      }
    }

    $('toTarget').onclick = () => {
      const value = $('sourceUrl').value.trim();
      if (!value) {
        setMessage('messageSource', 'Paste one source URL.', true);
        return;
      }
      pendingStyleId = '';
      pendingSource = value;
      setMessage('messageSource', '');
      screen('Target');
      $('subject').focus();
    };
    $('backToSource').onclick = () => screen('Source');
    $('newRun').onclick = () => {
      pendingStyleId = '';
      pendingSource = '';
      selectedCandidate = null;
      screen('Source');
      $('sourceUrl').focus();
    };
    $('prepare').onclick = prepare;
    $('refresh').onclick = () => refresh().catch((error) => setMessage('messageReview', error.message, true));
    $('confirmSource').onclick = confirmSource;
    $('startRuntime').onclick = startRuntime;
    $('loopAgain').onclick = loopAgain;
    $('approve').onclick = lock;

    refresh()
      .then(() => {
        if (view?.current_run) screen('Review');
      })
      .catch(() => {});
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "stylekit-ui/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/state":
            self.send_json(200, current_view())
            return
        if parsed.path == "/api/text":
            status, payload = text_payload(query.get("path", [None])[0])
            self.send_json(status, payload)
            return
        if parsed.path == "/api/file":
            try:
                path = repo_path(query.get("path", [None])[0])
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            if not path.exists() or not path.is_file():
                self.send_error(404, "file does not exist")
                return
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("content-type", mime)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404, "not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json()
        except json.JSONDecodeError as exc:
            self.send_json(400, {"status": "error", "error": str(exc)})
            return
        if parsed.path == "/api/prepare":
            status, response = prepare_run(payload)
            self.send_json(status, response)
            return
        if parsed.path == "/api/prepare-from-style":
            status, response = prepare_from_style(payload)
            self.send_json(status, response)
            return
        if parsed.path == "/api/lock":
            status, response = lock_style(payload)
            self.send_json(status, response)
            return
        if parsed.path == "/api/generate":
            status, response = start_runtime(payload)
            self.send_json(status, response)
            return
        if parsed.path == "/api/source-review":
            status, response = save_source_review(payload)
            self.send_json(status, response)
            return
        self.send_json(404, {"status": "error", "error": "not found"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"stylekit UI: {url}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
