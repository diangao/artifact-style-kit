# artifact-style-kit

Small utilities for turning a visual reference surface into an inspectable style pipeline:

1. collect asset URLs or local paths from saved source files
2. normalize them into a manifest
3. build contact sheets for visual comparison
4. remove flat chroma-key backgrounds from generated cutouts
5. keep prompt and taste notes as reviewable artifacts

The kit is intentionally generic. It does not encode a specific artist, site, or source.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Collect Assets

Collect image references from saved HTML, CSS, JS, JSON, or text files:

```bash
python scripts/collect_assets.py examples/source-page \
  --base-url https://example.com \
  --include "/assets/" \
  --manifest outputs/assets.json
```

Download matching assets:

```bash
python scripts/collect_assets.py examples/source-page \
  --base-url https://example.com \
  --include "/assets/" \
  --download-dir outputs/assets \
  --manifest outputs/assets.json
```

## Build A Contact Sheet

```bash
python scripts/build_contact_sheet.py \
  --input-dir outputs/assets \
  --output outputs/contact-sheet.jpg \
  --labels
```

## Make Transparent Cutouts

When generated images use a flat chroma-key background:

```bash
python scripts/chroma_to_alpha.py \
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

