#!/usr/bin/env python3
"""Run one agent iteration for the current style-kit run."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
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


def run_codex(run_name: str, run: dict, prompt_path: Path, log_path: Path, final_path: Path) -> int:
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
    with prompt_path.open() as stdin, log_path.open("a") as log:
        log.write(f"[{utc_now()}] starting {' '.join(cmd[:-1])} -\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=REPO_ROOT, stdin=stdin, stdout=log, stderr=subprocess.STDOUT, text=True)
        log.write(f"\n[{utc_now()}] exited {proc.returncode}\n")
    return proc.returncode


def run_claude(run_name: str, run: dict, prompt_path: Path, log_path: Path, final_path: Path) -> int:
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
    with prompt_path.open() as stdin, log_path.open("a") as log:
        log.write(f"[{utc_now()}] starting {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=REPO_ROOT, stdin=stdin, stdout=log, stderr=subprocess.STDOUT, text=True)
        log.write(f"\n[{utc_now()}] exited {proc.returncode}\n")
    if log_path.exists():
        final_path.write_text(log_path.read_text(errors="replace"))
    return proc.returncode


def run_runtime(runtime: str, run_name: str, run: dict, prompt_path: Path, log_path: Path, final_path: Path) -> int:
    if runtime == "codex":
        return run_codex(run_name, run, prompt_path, log_path, final_path)
    if runtime == "claude":
        return run_claude(run_name, run, prompt_path, log_path, final_path)
    log_path.write_text(f"unsupported runtime: {runtime}\n")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--runtime", default="codex", choices=["codex", "claude"])
    args = parser.parse_args()

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
        runtime_log=rel(log_path),
        runtime_final=rel(final_path),
        runtime_prompt=rel(prompt_path),
    )

    exit_code = run_runtime(args.runtime, args.run, run, prompt_path, log_path, final_path)
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
        runtime_log=rel(log_path),
        runtime_final=rel(final_path),
        has_generated=bool(generated),
        has_cutouts=bool(cutouts),
        has_comparison=comparison.exists(),
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
