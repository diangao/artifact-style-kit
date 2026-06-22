#!/usr/bin/env python3
"""Prepare a self-contained style run for a human or long-running agent."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from build_contact_sheet import build_sheet, image_paths, parse_color


@dataclass
class RunFiles:
    run_dir: str
    contact_sheet: str
    prompt: str
    taste_notes: str
    agent_brief: str
    generated_dir: str
    cutouts_dir: str


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-").lower()
    if not slug:
        raise ValueError("run name cannot be empty")
    return slug


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def prompt_text(subject: str, key_color: str) -> str:
    return f"""Use the contact sheet as visual style reference. Create one small standalone visual asset: {subject}.

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
"""


def taste_notes_text(subject: str) -> str:
    return f"""# Taste Notes

Subject: {subject}

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


def agent_brief_text(files: RunFiles, subject: str, key_color: str) -> str:
    return f"""# Agent Brief

Subject: {subject}

## Inspect First

1. Open `{files.contact_sheet}`.
2. Read `{files.prompt}`.
3. Generate candidate assets using the prompt.
4. Save generated chroma-key images in `{files.generated_dir}`.

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
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/runs"))
    parser.add_argument("--columns", type=int, default=8)
    parser.add_argument("--cell-size", type=int, default=160)
    parser.add_argument("--background", default="e8e9e0")
    parser.add_argument("--key", default="ff00ff")
    args = parser.parse_args()

    run_name = slugify(args.run_name)
    run_dir = args.output_dir / run_name
    generated_dir = run_dir / "generated"
    cutouts_dir = run_dir / "cutouts"
    generated_dir.mkdir(parents=True, exist_ok=True)
    cutouts_dir.mkdir(parents=True, exist_ok=True)

    paths = image_paths(args.reference_dir)
    if not paths:
        print(f"Error: no reference images found in {args.reference_dir}", file=sys.stderr)
        return 2

    contact_sheet = run_dir / "contact-sheet.jpg"
    build_sheet(
        paths=paths,
        output=contact_sheet,
        cell_size=args.cell_size,
        columns=args.columns,
        label_height=24,
        background=parse_color(args.background),
        labels=True,
        base_dir=args.reference_dir,
    )

    prompt_path = run_dir / "prompt.txt"
    taste_notes_path = run_dir / "taste-notes.md"
    agent_brief_path = run_dir / "agent-brief.md"

    files = RunFiles(
        run_dir=str(run_dir),
        contact_sheet=str(contact_sheet),
        prompt=str(prompt_path),
        taste_notes=str(taste_notes_path),
        agent_brief=str(agent_brief_path),
        generated_dir=str(generated_dir),
        cutouts_dir=str(cutouts_dir),
    )

    write_text(prompt_path, prompt_text(args.subject, args.key))
    write_text(taste_notes_path, taste_notes_text(args.subject))
    write_text(agent_brief_path, agent_brief_text(files, args.subject, args.key))

    run_json = run_dir / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "run_name": run_name,
                "subject": args.subject,
                "reference_dir": str(args.reference_dir),
                "reference_count": len(paths),
                "files": asdict(files),
                "next_action": f"Read {agent_brief_path} and generate candidates from {prompt_path}.",
            },
            indent=2,
        )
        + "\n"
    )

    print(json.dumps({"ok": True, "run": str(run_dir), "agent_brief": str(agent_brief_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

