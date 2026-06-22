# artifact-style-kit

Small utilities for turning a visual reference URL into an inspectable, agent-callable style pipeline:

1. collect image assets from one URL or saved source files
2. normalize them into a manifest
3. build contact sheets for visual comparison
4. remove flat chroma-key backgrounds from generated cutouts
5. prepare agent-ready run folders with prompts, notes, and next actions
6. keep prompt and taste notes as reviewable artifacts

The kit is intentionally generic. It does not encode a specific artist, site, or source.

## Human Quickstart

The cleanest use is to hand an agent this repo link, one source URL, and one target:

```text
Use this kit: https://github.com/diangao/artifact-style-kit
Source URL: https://example.com/style-source
Target: one ripe mango with a small green leaf
```

The agent should clone the repo, read `AGENTS.md`, prepare the run, generate candidates, and show you each iteration for approval.

If you are running the toolkit yourself, the underlying flow is:

1. Clone the repo.
2. Give it one source URL and one target asset.
3. Run `prepare_agent_run.py`.
4. Hand the generated `agent-brief.md` to any long-running agent runtime.
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
python3 scripts/prepare_agent_run.py \
  --run-name mango \
  --subject "one ripe mango with a small green leaf" \
  --source-url https://example.com \
  --include "/assets/"
```

This writes:

- `outputs/runs/mango/reference-assets/`
- `outputs/runs/mango/assets.json`
- `outputs/runs/mango/contact-sheet.jpg`
- `outputs/runs/mango/prompt.txt`
- `outputs/runs/mango/taste-notes.md`
- `outputs/runs/mango/agent-brief.md`
- `outputs/runs/mango/run.json`
- `.style-kit-state.json`

Give `agent-brief.md` to an agent. It contains the files to inspect, the prompt to use, the bounded stop rule, and the next commands to run after generation.

The minimum agent-facing input is:

```json
{
  "source_url": "https://example.com",
  "subject": "one ripe mango with a small green leaf"
}
```

You can pass it as a file:

```bash
python3 scripts/prepare_agent_run.py --target-json target.json --include "/assets/"
```

If the page is blocked, dynamic, or the asset collector is too broad, use the manual fallback:

```bash
mkdir -p data/reference-assets
# put reference PNG/JPG/WebP files in data/reference-assets

python3 scripts/prepare_agent_run.py \
  --run-name mango \
  --subject "one ripe mango with a small green leaf" \
  --reference-dir data/reference-assets
```

For the agent-facing contract, see `AGENTS.md`.
For the full input/output diagram, see `docs/agent-first-contract.md`.
For the planned human runtime UI, see `docs/human-runtime-ui-contract.md`.

## Collect Assets

Collect image references from one URL:

```bash
python3 scripts/collect_assets.py \
  --source-url https://example.com \
  --include "/assets/" \
  --manifest outputs/assets.json \
  --download-dir outputs/assets \
  --json
```

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

The v1 stop rule is deliberately bounded: run at most `--max-iterations` attempts, default `5`, or stop earlier when a human approves a candidate. Numeric drift scoring can be added later, but the first contract should not fake objectivity.

## Planned Human Runtime UI

The next layer should let a user choose a local agent runtime, recommend Codex when available, enter one source URL, and review artifacts after each iteration. Once a candidate is approved, that run becomes the locked style context for generating more objects or images.

That frontend should consume the same CLI + JSON + filesystem contract instead of introducing a separate hidden workflow.
