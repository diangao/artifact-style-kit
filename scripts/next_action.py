#!/usr/bin/env python3
"""Recommend the next command from the style-kit state file.

Tool contract:
- name: next_action
- purpose: let an agent resume a run without chat history
- inputs: .style-kit-state.json
- outputs: current run summary and recommended next action
- typical next tool: whichever command is recommended
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from stylekit_common import emit_json, load_state, ok_payload


def find_pngs(path: str | None) -> list[Path]:
    if not path:
        return []
    root = Path(path)
    if not root.exists():
        return []
    return sorted(root.glob("*.png"))


def recommend(run: dict) -> dict[str, str]:
    files = run.get("files", {})
    iteration = int(run.get("iteration", 1))
    max_iterations = int(run.get("max_iterations", 5))
    generated = find_pngs(files.get("generated_dir"))
    cutouts = find_pngs(files.get("cutouts_dir"))
    run_dir = Path(files.get("run_dir", "outputs/runs/current"))
    comparison = run_dir / "comparison.jpg"

    if iteration > max_iterations:
        return {
            "command": f"Stop iteration and update {files.get('taste_notes')} with best candidate plus remaining drift.",
            "why": f"Run iteration {iteration} is beyond max_iterations {max_iterations}.",
        }

    if not generated:
        source_review = files.get("source_review")
        if source_review and Path(source_review).exists():
            try:
                review = json.loads(Path(source_review).read_text())
            except json.JSONDecodeError:
                review = {}
            if review.get("status") != "confirmed":
                return {
                    "command": f"Review {source_review} and confirm/delete/supplement extracted elements before generation.",
                    "why": "The source evidence is collected, but the extraction review is not confirmed yet.",
                }
        return {
            "command": f"Read {files.get('agent_brief')} and generate candidates from {files.get('prompt')}.",
            "why": "The source extraction is confirmed and no generated candidates are present yet.",
        }

    if generated and not cutouts:
        first = generated[0]
        output = Path(files.get("cutouts_dir", str(run_dir / "cutouts"))) / f"{first.stem}-alpha.png"
        return {
            "command": f"python3 scripts/chroma_to_alpha.py --input {first} --output {output} --key ff00ff --json",
            "why": "Generated candidates exist but no transparent cutouts exist yet.",
        }

    if cutouts and not comparison.exists():
        return {
            "command": f"python3 scripts/build_contact_sheet.py --input-dir {files.get('cutouts_dir')} --output {comparison} --columns 4 --labels --json",
            "why": "Cutouts exist; build a comparison sheet for visual judgment.",
        }

    return {
        "command": f"Update {files.get('taste_notes')} with matches, drift, and next prompt constraints.",
        "why": "Comparison artifacts exist; the next step is taste judgment.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".style-kit-state.json"))
    parser.add_argument("--json", action="store_true", help="Accepted for consistency; output is always JSON.")
    args = parser.parse_args()

    state = load_state(args.state)
    current_run = state.get("current_run")
    run = state.get("runs", {}).get(current_run, {}) if current_run else {}
    action = recommend(run) if run else {
        "command": "python3 scripts/prepare_agent_run.py --run-name <name> --subject <subject> --source-url <url>",
        "why": "No current run exists in state. Start from one source URL and one target subject.",
    }

    emit_json(
        ok_payload(
            {
                "state": str(args.state),
                "current_run": current_run,
                "iteration": state.get("iteration", 0),
            },
            [action],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
