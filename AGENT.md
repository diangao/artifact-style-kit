# Agent Guide

This repository is an agent-facing toolkit for visual style runs.

Your job is not only to run scripts. Your job is to keep the loop inspectable:

```text
source URL -> reference assets -> contact sheet -> prompt -> generated image -> transparent cutout -> comparison -> taste notes -> next prompt
```

## Canonical Entry Point

Use `scripts/next_action.py` first. If there is no state file, it will tell you to create a run.

```bash
python3 scripts/next_action.py
```

To create a run:

```bash
python3 scripts/prepare_agent_run.py \
  --run-name <short-run-name> \
  --subject "<object or asset to generate>" \
  --source-url <url-with-style-reference-assets>
```

Then read the generated `outputs/runs/<run-name>/agent-brief.md`.

If URL collection fails because the page is blocked, dynamic, or too noisy, rerun with:

```bash
python3 scripts/prepare_agent_run.py \
  --run-name <short-run-name> \
  --subject "<object or asset to generate>" \
  --reference-dir <folder-with-reference-images>
```

## Agent-Readable Outputs

All primary tools support or produce JSON output. Prefer JSON mode when chaining tools:

```bash
python3 scripts/collect_assets.py --source-url <url> --manifest outputs/assets.json --download-dir outputs/assets --json
python3 scripts/build_contact_sheet.py --input-dir <dir> --output <sheet.jpg> --json
python3 scripts/chroma_to_alpha.py --input <image.png> --output <alpha.png> --json
python3 scripts/next_action.py --json
```

The common shape is:

```json
{
  "status": "ok",
  "data": {},
  "recommended_next": [
    { "command": "...", "why": "..." }
  ]
}
```

## What To Produce

Each run should end with these files:

- `reference-assets/` — downloaded reference images
- `assets.json` — collected source asset manifest
- `contact-sheet.jpg` — reference assets in one glanceable sheet
- `prompt.txt` — the current prompt used for generation
- `generated/` — generated candidates, if any
- `cutouts/` — transparent cutouts, if any
- `comparison.jpg` — optional sheet comparing generated assets
- `taste-notes.md` — what matches, what drifts, and next constraints
- `run.json` — machine-readable run metadata
- `.style-kit-state.json` — root state for resuming a run

## How To Judge A Candidate

Do not only ask "is it pretty?"

Check:

- object taxonomy: does the object belong to the reference world?
- silhouette: does it have the same scale and cutout feel?
- viewpoint: does it use the same camera angle?
- material: does it share the same texture/detail level?
- palette: is saturation and contrast aligned?
- edge treatment: does it feel like the same kind of asset?
- background contract: is it removable or already transparent?

## Stop Rule

Do not run an infinite refinement loop.

The v1 success gate is:

- stop early if a human approves a candidate
- otherwise stop after the run's `max_iterations`, default `5`
- save the best candidate and write the remaining drift in `taste-notes.md`

Do not invent a numeric drift score unless a real judge exists in the run.

## Tool Output Contract

When you add or change scripts, make outputs comfortable for agents:

- print the main output paths
- write a machine-readable JSON file when a run has state
- include the next action in the output or generated brief
- avoid raw dumps when a summary plus path is enough
- keep prompts and notes in files, not only chat

## Human Handoff

When reporting back to a human, include:

- the run folder path
- the generated asset path or attachment
- one sentence on what matches
- one sentence on what still drifts
- the next prompt constraint

Keep private source details out of public repos unless the human explicitly approves them.
