#!/usr/bin/env python3
"""Run one agent iteration for the current style-kit run."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from stylekit_common import load_state, save_state


REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = REPO_ROOT / ".style-kit-state.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def rel(path: Path) -> str:
    return str(path.resolve().relative_to(REPO_ROOT))


def pngs(path_value: str | None) -> list[Path]:
    if not path_value:
        return []
    path = REPO_ROOT / path_value
    if not path.exists():
        return []
    return sorted(path.glob("*.png"))


def update_run(run_name: str, **values: object) -> None:
    state = load_state(STATE_PATH)
    state.setdefault("runs", {}).setdefault(run_name, {}).update(values)
    state["updated_at"] = utc_now()
    save_state(state, STATE_PATH)


def runtime_prompt(run_name: str, run: dict, prompt_path: Path) -> None:
    files = run.get("files", {})
    text = f"""You are running one artifact-style-kit generation loop.

Repository: {REPO_ROOT}
Run: {run_name}
Subject: {run.get("subject")}

Read `{files.get("agent_brief")}` first and follow it exactly.

Complete one bounded iteration:
- inspect the reviewed source contract and contact sheet
- generate one reviewable chroma-key PNG candidate if your runtime has image generation available
- save generated PNGs under `{files.get("generated_dir")}`
- convert generated PNGs to alpha cutouts under `{files.get("cutouts_dir")}`
- build `{files.get("comparison")}`
- update `{files.get("taste_notes")}` with matches, drift, and next prompt constraints

If this runtime cannot generate images, do not fake an output. Write a clear blocker to
`{files.get("run_dir")}/runtime-error.md` and explain what capability is missing.
"""
    prompt_path.write_text(text)


TIMEOUT_EXIT_CODE = 124
HEARTBEAT_SECONDS = 15
TERMINATE_GRACE_SECONDS = 10


def terminate_process_group(proc: subprocess.Popen[str], log, timeout_seconds: int) -> int:
    log.write(f"\n[{utc_now()}] timed out after {timeout_seconds}s; terminating runtime process group\n")
    log.flush()
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        log.write(f"[{utc_now()}] runtime did not exit after SIGTERM; killing process group\n")
        log.flush()
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return TIMEOUT_EXIT_CODE
    except ProcessLookupError:
        return TIMEOUT_EXIT_CODE
    return TIMEOUT_EXIT_CODE


def run_command(cmd: list[str], prompt_path: Path, log_path: Path, timeout_seconds: int, run_name: str) -> int:
    with prompt_path.open() as stdin, log_path.open("a") as log:
        log.write(f"[{utc_now()}] starting {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdin=stdin,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        started = time.monotonic()
        next_heartbeat = started + HEARTBEAT_SECONDS
        update_run(run_name, runtime_pid=proc.pid, runtime_heartbeat_at=utc_now())
        while True:
            return_code = proc.poll()
            if return_code is not None:
                break

            now = time.monotonic()
            if now - started >= timeout_seconds:
                return_code = terminate_process_group(proc, log, timeout_seconds)
                break

            if now >= next_heartbeat:
                heartbeat_at = utc_now()
                log.write(f"[{heartbeat_at}] heartbeat: runtime still running pid={proc.pid}\n")
                log.flush()
                update_run(run_name, runtime_pid=proc.pid, runtime_heartbeat_at=heartbeat_at)
                next_heartbeat = now + HEARTBEAT_SECONDS

            time.sleep(1)
        log.write(f"\n[{utc_now()}] exited {return_code}\n")
        return return_code


def run_codex(run_name: str, run: dict, prompt_path: Path, log_path: Path, final_path: Path, timeout_seconds: int) -> int:
    binary = shutil.which("codex")
    if not binary:
        log_path.write_text("codex executable was not found on PATH.\n")
        return 127
    cmd = [
        binary,
        "exec",
        "--cd",
        str(REPO_ROOT),
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-last-message",
        str(final_path),
        "-",
    ]
    return run_command(cmd, prompt_path, log_path, timeout_seconds, run_name)


def run_claude(run_name: str, run: dict, prompt_path: Path, log_path: Path, final_path: Path, timeout_seconds: int) -> int:
    binary = shutil.which("claude")
    if not binary:
        log_path.write_text("claude executable was not found on PATH.\n")
        return 127
    cmd = [
        binary,
        "--print",
        "--permission-mode",
        "bypassPermissions",
    ]
    return_code = run_command(cmd, prompt_path, log_path, timeout_seconds, run_name)
    if log_path.exists():
        final_path.write_text(log_path.read_text(errors="replace"))
    return return_code


def run_runtime(
    runtime: str,
    run_name: str,
    run: dict,
    prompt_path: Path,
    log_path: Path,
    final_path: Path,
    timeout_seconds: int,
) -> int:
    if runtime == "codex":
        return run_codex(run_name, run, prompt_path, log_path, final_path, timeout_seconds)
    if runtime == "claude":
        return run_claude(run_name, run, prompt_path, log_path, final_path, timeout_seconds)
    log_path.write_text(f"unsupported runtime: {runtime}\n")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--runtime", default="codex", choices=["codex", "claude"])
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    args = parser.parse_args()
    if args.timeout_seconds < 60:
        parser.error("--timeout-seconds must be at least 60")

    state = load_state(STATE_PATH)
    run = state.get("runs", {}).get(args.run)
    if not run:
        print(f"run not found: {args.run}", file=sys.stderr)
        return 2

    files = run.get("files", {})
    run_dir = REPO_ROOT / files.get("run_dir", f"outputs/runs/{args.run}")
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "agent-runtime.log"
    final_path = run_dir / "agent-runtime-final.md"
    prompt_path = run_dir / "agent-runtime-prompt.md"
    runtime_prompt(args.run, run, prompt_path)

    update_run(
        args.run,
        status="generating",
        runtime=args.runtime,
        runtime_started_at=utc_now(),
        runtime_timeout_seconds=args.timeout_seconds,
        runtime_pid=None,
        runtime_heartbeat_at=None,
        runtime_log=rel(log_path),
        runtime_final=rel(final_path),
        runtime_prompt=rel(prompt_path),
    )

    exit_code = run_runtime(args.runtime, args.run, run, prompt_path, log_path, final_path, args.timeout_seconds)
    generated = pngs(files.get("generated_dir"))
    cutouts = pngs(files.get("cutouts_dir"))
    comparison = REPO_ROOT / files.get("comparison", "")

    if cutouts or generated:
        status = "candidate_ready"
    elif exit_code == 0:
        status = "source_reviewed"
    else:
        status = "runtime_failed"

    update_run(
        args.run,
        status=status,
        runtime_exit_code=exit_code,
        runtime_finished_at=utc_now(),
        runtime_pid=None,
        runtime_log=rel(log_path),
        runtime_final=rel(final_path),
        has_generated=bool(generated),
        has_cutouts=bool(cutouts),
        has_comparison=comparison.exists(),
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
