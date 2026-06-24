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
    runtime_pending = run.get("status") == "generating" and run.get("runtime_exit_code") is None
    if run.get("status") not in terminal_statuses and not runtime_pending and (has_cutouts or has_generated):
        updates["status"] = "candidate_ready"
        updates["recommended_next"] = {
            "command": "Review the candidate in the UI, then lock the style or loop again.",
            "why": "Generated files have landed in the run folder.",
        }
        if run.get("source_review_status") != "confirmed":
            updates["source_review_status"] = "confirmed"
            source_review = files.get("source_review")
            if source_review:
                try:
                    review_path = repo_path(source_review)
                    review = load_json(review_path)
                    review["status"] = "confirmed"
                    save_json(review_path, review)
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
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
            "runtime_exit_code": None,
            "runtime_finished_at": None,
            "runtime_pid": None,
            "runtime_heartbeat_at": None,
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
      --soft-blue: #e8edf3;
      --soft-green: #e6eee8;
      --soft-red: #f2e5df;
      --accent: #2f6f46;
      --accent-blue: #3b5f87;
      --accent-red: #9b4135;
      --ok: #226c4b;
      --bad: #a43434;
      --shadow: 0 18px 54px rgba(23,31,28,.10);
      --shadow-soft: 0 10px 28px rgba(23,31,28,.08);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(180deg, #fbfbf7 0%, #f0f2ec 56%, #e9eee9 100%);
      color: var(--ink);
    }
    button, input, textarea { font: inherit; }
    .app { width: min(1120px, calc(100vw - 32px)); margin: 0 auto; padding: 32px 0 56px; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 26px;
    }
    h1 { margin: 0; font-size: 18px; font-weight: 720; letter-spacing: 0; }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--ink);
    }
    .brand-mark {
      width: 28px;
      height: 28px;
      border: 1px solid rgba(23,31,28,.18);
      border-radius: 7px;
      background:
        linear-gradient(135deg, rgba(47,111,70,.96), rgba(47,111,70,.18) 42%, rgba(155,65,53,.88) 43%, rgba(155,65,53,.52) 70%, rgba(59,95,135,.72));
      box-shadow: inset 0 1px 0 rgba(255,255,255,.52), 0 8px 20px rgba(23,31,28,.10);
    }
    .step-count {
      color: var(--muted);
      font-size: 12px;
      border: 1px solid rgba(23,31,28,.10);
      border-radius: 999px;
      background: rgba(255,255,255,.58);
      padding: 7px 10px;
    }
    .screen { display: none; }
    .screen.active { display: block; }
    .prompt { max-width: 720px; margin: 0 auto; display: grid; gap: 18px; }
    .prompt h2 { margin: 0; font-size: clamp(34px, 6vw, 68px); line-height: 1.02; font-weight: 650; letter-spacing: 0; }
    .source-home {
      max-width: none;
      grid-template-columns: minmax(330px, .86fr) minmax(520px, 1.14fr);
      align-items: start;
      gap: 22px;
    }
    .source-intro,
    .source-examples {
      border: 1px solid rgba(23,31,28,.10);
      border-radius: 8px;
      background: rgba(255,255,255,.78);
      box-shadow: var(--shadow-soft);
    }
    .source-intro {
      display: grid;
      gap: 18px;
      padding: 24px;
      position: sticky;
      top: 18px;
    }
    .source-examples { padding: 18px; display: grid; gap: 16px; }
    .eyebrow {
      width: fit-content;
      border: 1px solid rgba(47,111,70,.24);
      border-radius: 999px;
      background: rgba(230,238,232,.72);
      color: var(--accent);
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 720;
    }
    .source-intro p {
      margin: 0;
      color: #4d5a55;
      line-height: 1.48;
      max-width: 42rem;
    }
    .source-copy-panel {
      display: grid;
      gap: 14px;
      border: 1px solid rgba(23,31,28,.10);
      border-radius: 8px;
      background: #fbfbf7;
      padding: 14px;
    }
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 12px;
    }
    .section-head h3 {
      margin: 0;
      font-size: 13px;
      color: var(--ink);
      font-weight: 760;
    }
    .section-head span {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
      text-align: right;
    }
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
      box-shadow: inset 0 1px 0 rgba(255,255,255,.75), 0 1px 0 rgba(0,0,0,.02);
    }
    textarea { min-height: 112px; resize: vertical; }
    input:focus, textarea:focus { border-color: #aeb8b2; box-shadow: 0 0 0 3px rgba(23,31,28,.06); }
    input:disabled, textarea:disabled, select:disabled { opacity: .55; cursor: not-allowed; }
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
    button.primary { background: var(--ink); border-color: var(--ink); color: white; box-shadow: 0 8px 24px rgba(23,31,28,.16); }
    button:hover:not(:disabled) { border-color: rgba(23,31,28,.28); transform: translateY(-1px); }
    button.primary:hover:not(:disabled) { background: #0f1513; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    button.loading {
      display: inline-flex;
      align-items: center;
      gap: 8px;
    }
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
    .style-lock {
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
    .seed-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .seed-card {
      display: grid;
      grid-template-columns: 86px 1fr;
      gap: 12px;
      min-height: 128px;
      padding: 10px;
      text-align: left;
      background: #fff;
      border-color: rgba(23,31,28,.12);
      box-shadow: none;
    }
    .seed-card:hover:not(:disabled) {
      box-shadow: 0 12px 26px rgba(23,31,28,.10);
    }
    .seed-preview {
      position: relative;
      height: 108px;
      border-radius: 7px;
      overflow: hidden;
      background:
        linear-gradient(180deg, rgba(255,255,255,.68), rgba(255,255,255,0)),
        var(--soft);
      border: 1px solid rgba(23,31,28,.08);
    }
    .seed-preview span,
    .seed-preview i,
    .seed-preview b,
    .seed-preview em {
      position: absolute;
      display: block;
      font-style: normal;
    }
    .seed-preview img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .seed-preview--cutout span {
      left: 14px; top: 32px; width: 58px; height: 44px;
      background: #b64131;
      clip-path: polygon(9% 28%, 56% 12%, 90% 34%, 78% 82%, 20% 88%);
      box-shadow: inset 10px 0 0 rgba(255,255,255,.10), inset -9px -8px 0 rgba(70,32,28,.28);
    }
    .seed-preview--cutout i {
      left: 22px; top: 24px; width: 38px; height: 20px;
      background: #d75a43;
      clip-path: polygon(8% 88%, 50% 0, 92% 82%, 72% 100%, 20% 100%);
    }
    .seed-preview--cutout b {
      left: 24px; top: 66px; width: 13px; height: 13px; border-radius: 99px;
      background: #2a2928; box-shadow: 31px 3px 0 #2a2928;
    }
    .seed-preview--poly span {
      left: 20px; top: 24px; width: 48px; height: 54px;
      background: #5c9b64;
      clip-path: polygon(49% 0, 100% 28%, 86% 86%, 42% 100%, 0 68%, 6% 18%);
      box-shadow: inset -14px -12px 0 rgba(28,78,48,.30), inset 14px 5px 0 rgba(255,255,255,.18);
    }
    .seed-preview--poly i {
      left: 36px; top: 16px; width: 26px; height: 24px;
      background: #f1cf69;
      clip-path: polygon(50% 0, 100% 35%, 82% 100%, 22% 92%, 0 28%);
    }
    .seed-preview--emoji span {
      left: 21px; top: 28px; width: 48px; height: 48px;
      border-radius: 50%;
      background: radial-gradient(circle at 32% 22%, #fff4ad 0 9%, transparent 10%), radial-gradient(circle at 36% 38%, #5b3d23 0 6%, transparent 7%), radial-gradient(circle at 64% 38%, #5b3d23 0 6%, transparent 7%), linear-gradient(135deg, #ffd35b, #f4a53d 72%);
      box-shadow: inset -10px -12px 0 rgba(130,74,22,.18), 0 8px 18px rgba(139,86,31,.22);
    }
    .seed-preview--kenney span {
      left: 18px; top: 32px; width: 54px; height: 44px;
      background: #78a7be;
      clip-path: polygon(10% 22%, 54% 0, 94% 24%, 90% 82%, 43% 100%, 0 76%);
      box-shadow: inset -16px -12px 0 rgba(31,72,94,.24);
    }
    .seed-preview--kenney i {
      left: 26px; top: 24px; width: 38px; height: 14px;
      background: #d8e5ea;
      clip-path: polygon(0 100%, 50% 0, 100% 100%);
    }
    .seed-preview--hand span {
      left: 24px; top: 18px; width: 40px; height: 56px;
      background: #b75537;
      border-radius: 50% 50% 45% 45%;
      box-shadow: inset -12px -10px 0 rgba(88,38,30,.24);
    }
    .seed-preview--hand i {
      left: 33px; top: 67px; width: 20px; height: 24px;
      background: #d1bc83;
      clip-path: polygon(22% 0, 88% 0, 100% 100%, 0 100%);
    }
    .seed-preview--line span {
      left: 18px; top: 24px; width: 52px; height: 52px;
      background: #f8f2d8;
      border: 2px solid #222;
      clip-path: polygon(10% 20%, 72% 4%, 94% 62%, 42% 100%, 0 72%);
      box-shadow: 5px 5px 0 rgba(47,111,70,.28);
    }
    .seed-preview--dither {
      background:
        radial-gradient(circle at 35% 30%, rgba(30,30,26,.92) 0 1px, transparent 1.6px) 0 0 / 7px 7px,
        linear-gradient(135deg, #f7efe1, #dac8a8);
    }
    .seed-preview--dither span {
      left: 15px; top: 18px; width: 58px; height: 72px;
      background:
        repeating-linear-gradient(0deg, rgba(23,31,28,.92) 0 1px, transparent 1px 5px),
        linear-gradient(135deg, #2a2a26, #725c3c);
      clip-path: polygon(0 10%, 86% 0, 100% 80%, 16% 100%);
      opacity: .88;
    }
    .seed-preview--dither i {
      left: 25px; top: 36px; width: 38px; height: 28px;
      background: radial-gradient(circle at 44% 48%, #f7efe1 0 2px, transparent 2.8px) 0 0 / 8px 8px;
    }
    .seed-preview--disco {
      background: linear-gradient(135deg, #121516, #3b332c 62%, #eee7d6);
    }
    .seed-preview--disco span {
      left: 18px; top: 18px; width: 54px; height: 54px; border-radius: 50%;
      background:
        linear-gradient(90deg, transparent 48%, rgba(30,30,30,.30) 49% 51%, transparent 52%),
        linear-gradient(0deg, transparent 48%, rgba(30,30,30,.30) 49% 51%, transparent 52%),
        conic-gradient(from 20deg, #f8f0c8, #b6e4e0, #f0938e, #f5df72, #ffffff, #8ec5ff, #f8f0c8);
      box-shadow: 0 12px 22px rgba(0,0,0,.32), inset -10px -12px 0 rgba(0,0,0,.18);
    }
    .seed-preview--disco i {
      left: 31px; top: 34px; width: 29px; height: 18px;
      background: rgba(20,24,24,.82);
      clip-path: polygon(0 36%, 100% 0, 80% 100%, 10% 82%);
    }
    .seed-preview--glass {
      background: radial-gradient(circle at 52% 45%, rgba(104,204,244,.28), transparent 42%), linear-gradient(145deg, #090d12, #171b23);
    }
    .seed-preview--glass span {
      left: 21px; top: 19px; width: 46px; height: 64px;
      background: linear-gradient(135deg, rgba(255,255,255,.82), rgba(124,225,255,.22) 35%, rgba(240,106,187,.32) 78%, rgba(255,255,255,.68));
      clip-path: polygon(50% 0, 94% 24%, 78% 100%, 20% 94%, 4% 26%);
      box-shadow: inset 11px 0 0 rgba(255,255,255,.22), inset -13px -16px 0 rgba(42,109,150,.25), 0 15px 26px rgba(0,0,0,.34);
    }
    .seed-preview--glass i {
      left: 16px; top: 48px; width: 56px; height: 2px;
      background: rgba(255,255,255,.75);
      transform: rotate(-18deg);
      box-shadow: 0 13px 0 rgba(120,220,255,.44);
    }
    .seed-preview--room {
      background: linear-gradient(180deg, #eaf1e7 0 54%, #c7b18f 55%);
    }
    .seed-preview--room span {
      left: 16px; top: 18px; width: 30px; height: 32px;
      background: #7fb7cd;
      border: 4px solid #fff8e7;
      box-shadow: 30px 42px 0 -8px #9f5b4e;
    }
    .seed-preview--room i {
      left: 44px; top: 52px; width: 24px; height: 31px;
      background: #355345;
      border-radius: 50% 50% 46% 46%;
      box-shadow: -22px 9px 0 -4px #e9c365;
    }
    .seed-preview--physical {
      background:
        linear-gradient(90deg, rgba(23,31,28,.06) 1px, transparent 1px) 0 0 / 16px 16px,
        linear-gradient(0deg, rgba(23,31,28,.06) 1px, transparent 1px) 0 0 / 16px 16px,
        #f6f2e8;
    }
    .seed-preview--physical span {
      left: 19px; top: 22px; width: 50px; height: 62px;
      background: #fff9df;
      border: 1px solid rgba(23,31,28,.22);
      transform: rotate(-7deg);
      box-shadow: 6px 8px 0 rgba(60,72,66,.12);
    }
    .seed-preview--physical i {
      left: 33px; top: 37px; width: 24px; height: 24px; border-radius: 50%;
      background: #e35f46;
      box-shadow: 0 0 0 5px rgba(227,95,70,.16);
    }
    .seed-preview--utility {
      background: linear-gradient(135deg, #f5f6f2, #d8e2db);
    }
    .seed-preview--utility span {
      left: 13px; top: 24px; width: 60px; height: 32px;
      border-radius: 999px;
      border: 1px solid rgba(23,31,28,.18);
      background: #fff;
      box-shadow: 0 10px 18px rgba(23,31,28,.12);
    }
    .seed-preview--utility i {
      left: 25px; top: 34px; width: 13px; height: 13px;
      border: 2px solid #26312d;
      border-radius: 50%;
    }
    .seed-preview--utility b {
      left: 38px; top: 47px; width: 10px; height: 2px;
      background: #26312d;
      transform: rotate(45deg);
      box-shadow: 16px -15px 0 #d95547, 23px -8px 0 #2f6f46;
    }
    .seed-preview--constellation {
      background: radial-gradient(circle at 50% 45%, rgba(255,255,255,.9), #eef3ef 62%, #dae4dc);
    }
    .seed-preview--constellation span {
      left: 15px; top: 24px; width: 16px; height: 16px; border-radius: 5px;
      background: #f3c75d;
      box-shadow: 35px -5px 0 #7fb7cd, 43px 40px 0 #c45d4b, 8px 49px 0 #4f7f63;
    }
    .seed-preview--constellation i {
      left: 22px; top: 31px; width: 48px; height: 46px;
      border-top: 1px solid rgba(23,31,28,.22);
      border-right: 1px solid rgba(23,31,28,.16);
      transform: rotate(10deg);
    }
    .seed-content {
      display: grid;
      align-content: start;
      gap: 6px;
      min-width: 0;
    }
    .seed-content strong {
      font-size: 14px;
      line-height: 1.16;
      color: var(--ink);
    }
    .seed-content p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .seed-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
      margin-top: 2px;
    }
    .seed-tags span {
      border: 1px solid rgba(23,31,28,.10);
      border-radius: 999px;
      background: #f8f8f3;
      color: #59635f;
      padding: 3px 6px;
      font-size: 10px;
      font-weight: 680;
    }
    .style-library {
      display: grid;
      gap: 10px;
      border-top: 1px solid rgba(23,31,28,.10);
      padding-top: 16px;
    }
    .style-library h3 { margin: 0; font-size: 13px; color: var(--ink); font-weight: 760; }
    .style-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; }
    .style-card {
      display: grid;
      grid-template-columns: 62px 1fr;
      gap: 12px;
      align-items: center;
      min-height: 86px;
      padding: 10px 12px;
      text-align: left;
      border-color: rgba(23,31,28,.12);
      background: linear-gradient(180deg, #fff, #f8f8f3);
    }
    .style-card img { width: 62px; height: 62px; object-fit: contain; border-radius: 7px; background: var(--soft); border: 1px solid rgba(23,31,28,.08); }
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
    .candidate-stage {
      position: relative;
      width: 100%;
      min-height: 520px;
      display: grid;
      place-items: center;
      background: var(--soft);
    }
    .candidate-stage a {
      width: 100%;
      display: grid;
      place-items: center;
    }
    .regen-status {
      position: absolute;
      top: 14px;
      left: 14px;
      right: 14px;
      z-index: 2;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border: 1px solid rgba(23,31,28,.14);
      border-radius: 8px;
      background: rgba(255,255,255,.94);
      box-shadow: 0 8px 24px rgba(0,0,0,.08);
      padding: 10px 12px;
      color: var(--ink);
      font-size: 13px;
    }
    .regen-status strong {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 650;
    }
    .runtime-detail {
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .old-candidate {
      position: absolute;
      right: 18px;
      bottom: 18px;
      z-index: 2;
      border: 1px solid rgba(23,31,28,.16);
      border-radius: 999px;
      background: rgba(255,255,255,.92);
      padding: 6px 10px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
    }
    .spinner {
      width: 14px;
      height: 14px;
      border: 2px solid rgba(23,31,28,.18);
      border-top-color: var(--ink);
      border-radius: 999px;
      animation: spin .8s linear infinite;
      flex: 0 0 auto;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
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
    .empty.busy {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      color: var(--ink);
      border-style: solid;
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
      .source-home { grid-template-columns: 1fr; }
      .source-intro { position: static; }
      .seed-grid { grid-template-columns: 1fr; }
      .artifact-main { min-height: 360px; }
      .candidate-stage { min-height: 360px; }
      .regen-status { align-items: flex-start; flex-direction: column; }
      .runtime-detail { white-space: normal; }
      .source-pass { grid-template-columns: 1fr; padding: 12px; }
      .prompt h2 { font-size: 38px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="brand">
        <span class="brand-mark" aria-hidden="true"></span>
        <h1>artifact-style-kit</h1>
      </div>
      <div id="stepCount" class="step-count">Step 1 of 2</div>
    </header>

    <section id="screenSource" class="screen active">
      <div class="prompt source-home">
        <div class="source-intro">
          <div class="eyebrow">Reusable object styles</div>
          <h2>Build a style you can reuse.</h2>
          <p>Start with a source page or one of the visual-treatment seeds. The first pass extracts visual evidence; once a result works, lock it as a named style and reuse it for another target.</p>
          <div class="source-copy-panel">
            <div class="field">
              <label for="sourceUrl">Source URL</label>
              <input id="sourceUrl" placeholder="https://example.com/style-source" autocomplete="off" autofocus>
            </div>
            <div class="actions">
              <button id="toTarget" class="primary">Next</button>
            </div>
            <div id="messageSource" class="message"></div>
          </div>
        </div>
        <div class="source-examples">
          <div class="section-head">
            <h3>Style seeds to copy</h3>
            <span>Click one to start from a direction.</span>
          </div>
          <div id="styleSeeds" class="seed-grid"></div>
          <div id="styleLibrary" class="style-library" hidden></div>
        </div>
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
            <label id="styleLock" class="style-lock" for="styleName">
              Name
              <input id="styleName" class="style-name" placeholder="e.g. raft pixel daisy">
            </label>
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
    let candidatePinned = false;
    let pendingSource = '';
    let pendingStyleId = '';
    let runtimePollTimer = null;
    let runtimeWasBusy = false;
    const STYLE_SEEDS = [
      {
        id: 'faceted-cutout',
        label: 'Faceted object cutouts',
        url: '',
        note: 'Faceted hand-painted objects, transparent cutouts, toy-like silhouettes.',
        tags: ['faceted', 'cutout', 'personal site'],
        preview: 'cutout',
        cover: 'outputs/runs/seed-cover-faceted-cutouts/generated/faceted-cutouts-mango-cover.png'
      },
      {
        id: 'dither-cards',
        label: 'Dither card system',
        url: 'https://contra.com/community/pHXPsYuS-transform-your-designs-mastering-dither-ascii',
        note: 'Low-ink dither, ASCII texture, motion-ready cards, and strict graphic logic.',
        tags: ['dither', 'ASCII', 'cards'],
        preview: 'dither',
        cover: 'outputs/runs/seed-cover-dither-card-system/generated/dither-card-system-mango-cover.png'
      },
      {
        id: 'disco-shell',
        label: 'Disco-shell icon',
        url: 'https://www.racejohnson.com/projects/discomorphism',
        note: 'A logo becomes a hard mirrored object with square facets and app-icon punch.',
        tags: ['icon', 'mirror', 'remix'],
        preview: 'disco',
        cover: 'outputs/runs/seed-cover-disco-shell-icon/generated/disco-shell-icon-mango-cover.png'
      },
      {
        id: 'prismatic-glass',
        label: 'Prismatic glass object',
        url: 'https://x.com/poletaeviktor/status/2069484424844960190',
        note: 'Dark-stage refraction: one emblem becomes a luminous glass sculpture.',
        tags: ['glass', 'logo', 'lighting'],
        preview: 'glass',
        cover: 'outputs/runs/seed-cover-prismatic-glass-object/generated/prismatic-glass-object-mango-cover.png'
      },
      {
        id: 'digital-room',
        label: 'Whimsical digital room',
        url: 'https://www.aileenis.online/',
        note: 'Personal homepage as a small inhabited room: windows, props, soft scenes.',
        tags: ['personal', 'room', 'props'],
        preview: 'room',
        cover: 'outputs/runs/seed-cover-whimsical-digital-room/generated/whimsical-digital-room-mango-cover.png'
      },
      {
        id: 'physical-ui',
        label: 'Physical-metaphor UI',
        url: 'https://ryanstephen.co/',
        note: 'Digital functions become clocks, paper, folders, grass, and spatial objects.',
        tags: ['spatial', 'analog', 'interface'],
        preview: 'physical',
        cover: 'outputs/runs/seed-cover-physical-metaphor-ui/generated/physical-metaphor-ui-mango-cover.png'
      },
      {
        id: 'utility-affordance',
        label: 'Animated utility affordance',
        url: 'https://lab01.dev/#ui-experiment',
        note: 'Small controls with exact icons, fonts, colors, and motion constraints.',
        tags: ['control', 'motion', 'tokens'],
        preview: 'utility',
        cover: 'outputs/runs/seed-cover-animated-utility-affordance/generated/animated-utility-affordance-mango-cover.png'
      },
      {
        id: 'object-constellation',
        label: 'Object constellation UI',
        url: 'https://feather.computer/',
        note: 'A workspace turns messages or files into sparse floating object clusters.',
        tags: ['inbox', 'objects', 'canvas'],
        preview: 'constellation',
        cover: 'outputs/runs/seed-cover-object-constellation-ui/generated/object-constellation-ui-mango-cover.png'
      }
    ];

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

    function candidateRenderKey(item) {
      const rawName = item?.name || String(item?.path || '').split('/').pop() || '';
      return rawName
        .replace(/\.[^.]+$/, '')
        .replace(/-alpha$/i, '')
        .replace(/-transparent$/i, '');
    }

    function candidateSortKey(item) {
      const key = candidateRenderKey(item);
      const match = key.match(/(\d+)(?!.*\d)/);
      return {
        index: match ? Number(match[1]) : Number.MAX_SAFE_INTEGER,
        key,
        path: item?.path || ''
      };
    }

    function compareCandidates(a, b) {
      const left = candidateSortKey(a);
      const right = candidateSortKey(b);
      if (left.index !== right.index) return left.index - right.index;
      const byKey = left.key.localeCompare(right.key, undefined, { numeric: true, sensitivity: 'base' });
      if (byKey) return byKey;
      return left.path.localeCompare(right.path, undefined, { numeric: true, sensitivity: 'base' });
    }

    function candidates() {
      const byRender = new Map();
      const generated = [...(view?.artifacts?.generated || [])].sort(compareCandidates);
      const cutouts = [...(view?.artifacts?.cutouts || [])].sort(compareCandidates);
      for (const item of generated) {
        byRender.set(candidateRenderKey(item), { ...item, candidate_kind: 'generated' });
      }
      for (const item of cutouts) {
        const key = candidateRenderKey(item);
        byRender.set(key, { ...item, candidate_kind: 'transparent' });
      }
      return [...byRender.values()].sort(compareCandidates);
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

    function isRuntimeBusy() {
      return isGenerating() || Boolean(view?.runtime_active);
    }

    function relativeTime(value) {
      if (!value) return '';
      const timestamp = Date.parse(value);
      if (Number.isNaN(timestamp)) return value;
      const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
      if (seconds < 5) return 'just now';
      if (seconds < 60) return `${seconds}s ago`;
      const minutes = Math.round(seconds / 60);
      if (minutes < 60) return `${minutes}m ago`;
      const hours = Math.round(minutes / 60);
      return `${hours}h ago`;
    }

    function runtimeDetailText() {
      const run = view?.run || {};
      const parts = [];
      if (view?.current_run) parts.push(view.current_run);
      if (run.runtime) parts.push(run.runtime);
      if (run.runtime_heartbeat_at) {
        parts.push(`heartbeat ${relativeTime(run.runtime_heartbeat_at)}`);
      } else if (run.runtime_started_at) {
        parts.push(`started ${relativeTime(run.runtime_started_at)}`);
      } else {
        parts.push('heartbeat pending');
      }
      return parts.join(' · ');
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
      return selectedCandidate || items.at(-1)?.path || null;
    }

    async function loadText(path) {
      if (!path) return 'No file yet.';
      const data = await api(`/api/text?path=${encodeURIComponent(path)}`);
      return data.text;
    }

    async function refresh() {
      view = await api('/api/state');
      const run = view.run || {};
      const busy = isRuntimeBusy();
      const status = busy ? 'regenerating' : (run.status || 'prepared');
      $('runMeta').textContent = view.current_run ? `${view.current_run} · ${status}` : 'No run yet';
      const items = candidates();
      const paths = new Set(items.map((item) => item.path));
      if (candidatePinned && selectedCandidate && !paths.has(selectedCandidate)) {
        candidatePinned = false;
        selectedCandidate = null;
      }
      if (!candidatePinned) {
        selectedCandidate = busy ? null : (items.at(-1)?.path || null);
      }
      if (runtimeWasBusy && !busy && items.length && !$('messageReview').classList.contains('error')) {
        setMessage('messageReview', 'Refinement complete. Reviewing the newest candidate.');
      }
      runtimeWasBusy = busy;
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

    function renderStyleSeeds() {
      const root = $('styleSeeds');
      root.innerHTML = STYLE_SEEDS.map((seed) => `
        <button class="seed-card" data-seed="${escapeHtml(seed.id)}" title="${escapeHtml(seed.url || seed.label)}">
          <span class="seed-preview seed-preview--${escapeHtml(seed.preview)}" aria-hidden="true">
            ${seed.cover
              ? `<img src="${fileUrl(seed.cover)}" alt="">`
              : '<span></span><i></i><b></b><em></em>'}
          </span>
          <span class="seed-content">
            <strong>${escapeHtml(seed.label)}</strong>
            <p>${escapeHtml(seed.note)}</p>
            <span class="seed-tags">${seed.tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join('')}</span>
          </span>
        </button>
      `).join('');
      document.querySelectorAll('[data-seed]').forEach((button) => {
        button.onclick = () => {
          const seed = STYLE_SEEDS.find((item) => item.id === button.dataset.seed);
          if (!seed) return;
          pendingStyleId = '';
          pendingSource = seed.url || '';
          $('sourceUrl').value = seed.url || '';
          setMessage(
            'messageSource',
            seed.url ? `${seed.label} loaded. Describe a target next.` : `${seed.label} selected. Paste a source URL when ready.`
          );
        };
      });
    }

    function syncRuntimePolling() {
      if (isRuntimeBusy()) {
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
      const busy = isRuntimeBusy();
      const canStartRuntime = !path && !needsReview && view?.source_review?.status === 'confirmed' && !busy && Boolean(defaultRuntimeName());
      $('reviewTitle').textContent = path
        ? busy
          ? 'Regenerating candidate.'
          : 'Review the result.'
        : needsReview
          ? 'Review the extracted elements.'
          : busy
            ? 'Generating candidate.'
            : 'Waiting for agent output.';
      $('approve').hidden = !path;
      $('approve').disabled = !path || busy;
      $('loopAgain').hidden = !path;
      $('loopAgain').disabled = !path || busy;
      $('loopAgain').classList.toggle('loading', Boolean(path && busy));
      $('loopAgain').innerHTML = path && busy ? '<span class="spinner"></span> Regenerating...' : 'Loop again';
      $('styleLock').hidden = !path;
      $('styleName').disabled = !path || busy;
      $('newRun').disabled = busy;
      if (path && $('styleName').dataset.run !== view.current_run) {
        $('styleName').value = run.subject || view.current_run || '';
        $('styleName').dataset.run = view.current_run || '';
      }
      if (!path) {
        $('styleName').dataset.run = '';
      }
      $('confirmSource').hidden = !needsReview;
      $('confirmSource').disabled = !needsReview;
      $('runtimePicker').hidden = !canStartRuntime && !(!path && busy);
      renderRuntimeSelect(canStartRuntime);
      $('startRuntime').hidden = !canStartRuntime && !(!path && busy);
      $('startRuntime').disabled = !canStartRuntime;
      $('startRuntime').classList.toggle('loading', Boolean(!path && busy));
      $('startRuntime').innerHTML = !path && busy ? '<span class="spinner"></span> Generating...' : 'Start runtime';
      if (needsReview) {
        renderSourceReview();
        return;
      }
      if (!path) {
        renderWaitingForGeneration();
        return;
      }
      $('artifactMain').innerHTML = `
        <div class="candidate-stage">
          ${busy ? `
            <div class="regen-status" role="status" aria-live="polite">
              <strong><span class="spinner"></span> Regenerating</strong>
              <span class="runtime-detail">${escapeHtml(runtimeDetailText())}</span>
            </div>
            <div class="old-candidate">old candidate</div>
          ` : ''}
          <a href="${fileUrl(path)}" target="_blank"><img src="${fileUrl(path)}" alt="${path}"></a>
        </div>
      `;
    }

    function renderWaitingForGeneration() {
      const sheet = view.files?.review_contact_sheet || view.files?.contact_sheet;
      const busy = isRuntimeBusy();
      const text = busy
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
          <div class="empty ${busy ? 'busy' : ''}">
            ${busy ? '<span class="spinner"></span>' : ''}
            <span>${escapeHtml(text)}${busy ? `<br>${escapeHtml(runtimeDetailText())}` : ''}</span>
          </div>
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
      const selected = selectedArtifact();
      $('thumbs').innerHTML = items.map((item) => `
        <button class="thumb ${item.path === selected ? 'selected' : ''}" data-candidate="${item.path}" title="${item.name}">
          <img src="${fileUrl(item.path)}" alt="${item.name}">
        </button>
      `).join('');
      document.querySelectorAll('[data-candidate]').forEach((button) => {
        button.onclick = () => {
          selectedCandidate = button.dataset.candidate;
          candidatePinned = true;
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
        candidatePinned = false;
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
      const styleName = $('styleName').value.trim();
      if (!styleName) {
        setMessage('messageReview', 'Name this style before locking it.', true);
        $('styleName').focus();
        return;
      }
      $('approve').disabled = true;
      try {
        const data = await api('/api/lock', {
          method: 'POST',
          body: JSON.stringify({
            candidate_path: selectedCandidate,
            style_name: styleName
          })
        });
        await refresh();
        screen('Source');
        $('sourceUrl').value = '';
        pendingSource = '';
        pendingStyleId = '';
        const lockedName = data.locked_style?.name || styleName;
        setMessage('messageReview', '');
        setMessage('messageSource', `Saved style "${lockedName}".`);
      } catch (error) {
        setMessage('messageReview', error.message, true);
      } finally {
        $('approve').disabled = false;
      }
    }

    async function loopAgain() {
      $('loopAgain').disabled = true;
      selectedCandidate = null;
      candidatePinned = false;
      const runtime = $('runtimeSelect').value || defaultRuntimeName();
      setMessage('messageReview', `Starting another ${runtime} pass...`);
      try {
        await api('/api/generate', {
          method: 'POST',
          body: JSON.stringify({runtime})
        });
        await refresh();
        setMessage('messageReview', 'Refinement started. This page will keep polling and switch to the newest candidate when it lands.');
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
        selectedCandidate = null;
        candidatePinned = false;
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
      selectedCandidate = null;
      candidatePinned = false;
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
      candidatePinned = false;
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
        renderStyleSeeds();
        if (view?.current_run && view?.run?.status !== 'locked') screen('Review');
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
