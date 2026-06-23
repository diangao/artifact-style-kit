#!/usr/bin/env python3
"""Prepare a self-contained style run for a human or long-running agent.

Tool contract:
- name: prepare_agent_run
- purpose: create a run folder with reference sheet, prompt, taste notes, agent brief, and run metadata
- inputs: source URL or reference image directory, target subject or target JSON
- outputs: outputs/runs/<run-name>/ plus .style-kit-state.json
- typical next tool: next_action.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from build_contact_sheet import build_sheet, image_paths, parse_color
from collect_assets import (
    DEFAULT_ASSET_RE,
    collect_from_url,
    compile_filters,
    download_assets,
    write_manifest,
)
from stylekit_common import emit_json, ok_payload, update_run_state


@dataclass
class RunFiles:
    run_dir: str
    reference_dir: str
    assets_manifest: str | None
    contact_sheet: str
    source_review: str
    prompt: str
    taste_notes: str
    agent_brief: str
    generated_dir: str
    cutouts_dir: str
    comparison: str


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-").lower()
    if not slug:
        raise ValueError("run name cannot be empty")
    return slug


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def load_target(path: Path | None) -> dict:
    if not path:
        return {}
    return json.loads(path.read_text())


def coerce_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def prompt_text(subject: str, key_color: str, max_iterations: int, source_review: str) -> str:
    return f"""Use the contact sheet as visual evidence, then follow the reviewed source-element contract in `{source_review}`. Create one small standalone visual asset: {subject}.

Before generation:
- Read the source review.
- Use the confirmed contact-sheet elements as the evidence set.
- Treat `ignored_reference_assets` and `missing_reference_notes` as constraints.
- If the source review is still `draft`, stop and ask for a human extraction pass before generating.

Style constraints:
- handmade object asset
- compact centered cutout
- consistent viewpoint with the reference sheet
- aligned palette, texture, detail level, and edge treatment
- generous padding around the subject

Avoid:
- photorealism unless the reference is photorealistic
- glossy over-rendered 3D unless the reference is glossy
- generic stock icon feel
- scene background
- text, labels, watermark, or logo marks unless requested

Background contract:
- Put the subject on a perfectly flat solid #{key_color} chroma-key background for background removal.
- Do not use #{key_color} anywhere in the subject.
- No cast shadow, gradient, texture, floor plane, or reflection in the background.

Loop contract:
- Iterate at most {max_iterations} times before stopping for review.
- Prefer a clearly reviewable candidate over endless refinement.
"""


def source_review_text(subject: str, source_url: str | None) -> str:
    return json.dumps(
        {
            "status": "draft",
            "subject": subject,
            "source_url": source_url or "manual reference folder",
            "review_goal": "Confirm that the extracted elements/contact sheet are the right source evidence.",
            "ignored_reference_assets": [],
            "missing_reference_notes": "",
            "style_notes": "",
        },
        indent=2,
    ) + "\n"


def taste_notes_text(subject: str, source_url: str | None, max_iterations: int) -> str:
    return f"""# Taste Notes

Subject: {subject}
Source URL: {source_url or "manual reference folder"}
Max iterations: {max_iterations}

## What Matches

- 

## What Drifts

- 

## Next Prompt Constraints

- 

## Decision

- [ ] keep
- [ ] revise
- [ ] discard
"""


def agent_brief_text(files: RunFiles, subject: str, key_color: str, source_url: str | None, max_iterations: int) -> str:
    return f"""# Agent Brief

Subject: {subject}
Source URL: {source_url or "manual reference folder"}
Stop rule: stop after {max_iterations} iterations or when a human approves a candidate.

## Inspect First

1. Open `{files.contact_sheet}`.
2. Read `{files.source_review}`.
3. If the source review is still `draft`, stop for human extraction review.
4. Use the confirmed elements; ignore `ignored_reference_assets` and account for `missing_reference_notes`.
5. Read `{files.prompt}`.
6. Generate candidate assets using the prompt.
7. Save generated chroma-key images in `{files.generated_dir}`.

## After Generation

Convert each chroma-key result to alpha:

```bash
python3 scripts/chroma_to_alpha.py \\
  --input {files.generated_dir}/<candidate>.png \\
  --output {files.cutouts_dir}/<candidate>-alpha.png \\
  --key {key_color}
```

Build a comparison sheet:

```bash
python3 scripts/build_contact_sheet.py \\
  --input-dir {files.cutouts_dir} \\
  --output {Path(files.run_dir) / "comparison.jpg"} \\
  --columns 4 \\
  --labels
```

Then update `{files.taste_notes}` with:

- what matches
- what drifts
- the next prompt constraint

## Judgment Rule

Prefer visible similarity to the reference sheet over generic polish. If a candidate is prettier but less aligned, mark the drift explicitly.

Do not treat the raw contact sheet as the whole spec. The source review chooses which extracted elements are valid evidence.

Do not loop forever. If the candidate still drifts after {max_iterations} iterations, save the best candidate and flag the remaining drift for human review.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name")
    parser.add_argument("--subject")
    parser.add_argument("--target-json", type=Path, help="Optional JSON file with subject/source_url/run_name.")
    parser.add_argument("--source-url", help="One URL to fetch and extract style reference assets from.")
    parser.add_argument("--reference-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/runs"))
    parser.add_argument("--columns", type=int, default=8)
    parser.add_argument("--cell-size", type=int, default=160)
    parser.add_argument("--background", default="e8e9e0")
    parser.add_argument("--key", default="ff00ff")
    parser.add_argument("--include", action="append", default=[], help="Regex asset include filter. May be repeated.")
    parser.add_argument("--exclude", action="append", default=[], help="Regex asset exclusion filter. May be repeated.")
    parser.add_argument("--asset-regex", default=DEFAULT_ASSET_RE.pattern)
    parser.add_argument("--max-assets", type=int, default=60)
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--user-agent", default="artifact-style-kit/1.0")
    parser.add_argument("--state", type=Path, default=Path(".style-kit-state.json"))
    args = parser.parse_args()

    target = load_target(args.target_json)
    subject = args.subject or target.get("subject")
    if not subject:
        parser.error("provide --subject or a target JSON with a subject")

    source_url = args.source_url or target.get("source_url")
    reference_dir = args.reference_dir or (Path(target["reference_dir"]) if target.get("reference_dir") else None)
    if not source_url and not reference_dir:
        parser.error("provide --source-url or --reference-dir")

    run_name = slugify(args.run_name or target.get("run_name") or subject)
    max_iterations = int(target.get("max_iterations") or args.max_iterations)
    include_patterns = args.include + coerce_list(target.get("include"))
    exclude_patterns = args.exclude + coerce_list(target.get("exclude"))

    run_dir = args.output_dir / run_name
    assets_manifest: Path | None = None
    download_errors: list[dict[str, str]] = []
    if source_url:
        try:
            asset_re = re.compile(args.asset_regex, re.IGNORECASE)
            includes = compile_filters(include_patterns)
            excludes = compile_filters(exclude_patterns)
            assets = collect_from_url(source_url, asset_re, includes, excludes, args.user_agent)
        except Exception as exc:
            print(f"Error: failed to collect assets from {source_url}: {exc}", file=sys.stderr)
            print("Next action: rerun with --reference-dir pointing at manually collected reference images.", file=sys.stderr)
            return 2
        if args.max_assets > 0:
            assets = assets[: args.max_assets]
        reference_dir = run_dir / "reference-assets"
        assets_manifest = run_dir / "assets.json"
        write_manifest(assets, assets_manifest)
        download_errors = download_assets(assets, reference_dir)

    assert reference_dir is not None
    generated_dir = run_dir / "generated"
    cutouts_dir = run_dir / "cutouts"
    generated_dir.mkdir(parents=True, exist_ok=True)
    cutouts_dir.mkdir(parents=True, exist_ok=True)

    paths = image_paths(reference_dir)
    if not paths:
        print(f"Error: no reference images found in {reference_dir}", file=sys.stderr)
        print("Next action: provide a URL with direct image assets or use --reference-dir.", file=sys.stderr)
        return 2

    contact_sheet = run_dir / "contact-sheet.jpg"
    comparison = run_dir / "comparison.jpg"
    build_sheet(
        paths=paths,
        output=contact_sheet,
        cell_size=args.cell_size,
        columns=args.columns,
        label_height=24,
        background=parse_color(args.background),
        labels=True,
        base_dir=reference_dir,
    )

    prompt_path = run_dir / "prompt.txt"
    source_review_path = run_dir / "source-review.json"
    taste_notes_path = run_dir / "taste-notes.md"
    agent_brief_path = run_dir / "agent-brief.md"

    files = RunFiles(
        run_dir=str(run_dir),
        reference_dir=str(reference_dir),
        assets_manifest=str(assets_manifest) if assets_manifest else None,
        contact_sheet=str(contact_sheet),
        source_review=str(source_review_path),
        prompt=str(prompt_path),
        taste_notes=str(taste_notes_path),
        agent_brief=str(agent_brief_path),
        generated_dir=str(generated_dir),
        cutouts_dir=str(cutouts_dir),
        comparison=str(comparison),
    )

    write_text(source_review_path, source_review_text(subject, source_url))
    write_text(prompt_path, prompt_text(subject, args.key, max_iterations, str(source_review_path)))
    write_text(taste_notes_path, taste_notes_text(subject, source_url, max_iterations))
    write_text(agent_brief_path, agent_brief_text(files, subject, args.key, source_url, max_iterations))

    run_json = run_dir / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "run_name": run_name,
                "subject": subject,
                "source_url": source_url,
                "reference_dir": str(reference_dir),
                "reference_count": len(paths),
                "download_error_count": len(download_errors),
                "download_errors": download_errors,
                "max_iterations": max_iterations,
                "files": asdict(files),
                "next_action": f"Review {source_review_path} and confirm/delete/supplement extracted elements before generation.",
            },
            indent=2,
        )
        + "\n"
    )

    update_run_state(
        run_name,
        {
            "iteration": 1,
            "subject": subject,
            "source_url": source_url,
            "reference_dir": str(reference_dir),
            "reference_count": len(paths),
            "download_error_count": len(download_errors),
            "download_errors": download_errors,
            "max_iterations": max_iterations,
            "status": "prepared",
            "source_review_status": "draft",
            "files": asdict(files),
            "recommended_next": {
                "command": f"Review {source_review_path} and confirm/delete/supplement extracted elements before generation.",
                "why": "The run is prepared; the next step is validating the extracted source evidence.",
            },
        },
        args.state,
    )

    emit_json(
        ok_payload(
            {
                "run": str(run_dir),
                "agent_brief": str(agent_brief_path),
                "state": str(args.state),
            },
            [
                {
                    "command": f"python3 scripts/next_action.py --state {args.state}",
                    "why": "Ask the toolkit what the agent should do next.",
                }
            ],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
