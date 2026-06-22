# artifact-style-kit

Small utilities for turning a visual reference surface into an inspectable style pipeline:

1. collect asset URLs or local paths from saved source files
2. normalize them into a manifest
3. build contact sheets for visual comparison
4. remove flat chroma-key backgrounds from generated cutouts
5. prepare agent-ready run folders with prompts, notes, and next actions
6. keep prompt and taste notes as reviewable artifacts

The kit is intentionally generic. It does not encode a specific artist, site, or source.

## Human Quickstart

Human-facing use is not "be the loop yourself." The intended flow is:

1. Clone the repo.
2. Put reference images in a folder.
3. Run `prepare_agent_run.py`.
4. Hand the generated `agent-brief.md` to a long-running agent.
5. Review the generated assets and taste notes after each iteration.
6. If the agent loses context, tell it to run `python3 scripts/next_action.py`.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Prepare An Agent Run

```bash
mkdir -p data/reference-assets
# put reference PNG/JPG/WebP files in data/reference-assets

python3 scripts/prepare_agent_run.py \
  --run-name mango \
  --subject "one ripe mango with a small green leaf" \
  --reference-dir data/reference-assets
```

This writes:

- `outputs/runs/mango/contact-sheet.jpg`
- `outputs/runs/mango/prompt.txt`
- `outputs/runs/mango/taste-notes.md`
- `outputs/runs/mango/agent-brief.md`
- `outputs/runs/mango/run.json`

Give `agent-brief.md` to an agent. It contains the files to inspect, the prompt to use, and the next commands to run after generation.

For the agent-facing contract, see `AGENT.md`.
For the full input/output diagram, see `docs/agent-first-contract.md`.

## Collect Assets

Collect image references from saved HTML, CSS, JS, JSON, or text files:

```bash
python3 scripts/collect_assets.py examples/source-page \
  --base-url https://example.com \
  --include "/assets/" \
  --manifest outputs/assets.json
```

Download matching assets:

```bash
python3 scripts/collect_assets.py examples/source-page \
  --base-url https://example.com \
  --include "/assets/" \
  --download-dir outputs/assets \
  --manifest outputs/assets.json
```

## Build A Contact Sheet

```bash
python3 scripts/build_contact_sheet.py \
  --input-dir outputs/assets \
  --output outputs/contact-sheet.jpg \
  --labels
```

## Make Transparent Cutouts

When generated images use a flat chroma-key background:

```bash
python3 scripts/chroma_to_alpha.py \
  --input outputs/generated-on-magenta.png \
  --output outputs/generated-alpha.png \
  --key ff00ff
```

## Taste Loop

This repository does not pretend the taste check is automatic. The inspectable loop is:

```text
source assets -> contact sheet -> prompt recipe -> generation -> contact sheet comparison -> notes -> next prompt
```

Keep prompts in `prompts/` and comparisons in `outputs/` so each iteration can be reviewed.
