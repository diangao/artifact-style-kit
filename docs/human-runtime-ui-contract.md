# Human Runtime UI Contract

The human-facing layer should be another consumer of the same agent-first toolkit contract.

It should not replace the CLI primitives. It should help a user run them through an installed local agent runtime and make each iteration visible.

Current local entry point:

```bash
python3 scripts/stylekit_ui.py
```

This starts a local browser UI backed by the existing scripts and `.style-kit-state.json`.

## Intended Flow

```text
CLONE + RUN
  user clones the repo
  user starts the local launcher/UI

        v

RUNTIME READY
  launcher detects installed agent CLIs on PATH
  runtime details stay hidden unless needed

        v

STEP 1: SOURCE
  user enters one source URL

        v

STEP 2: TARGET
  user enters one target object or image request
  launcher calls prepare_agent_run.py

        v

REVIEW RESULT
  launcher shows the primary candidate, comparison, or waiting state
  user approves, refreshes, or starts a new run

        v

STYLE LOCKED
  approved run is persisted as locked_style
  future targets reuse locked_style without rediscovering the URL
```

## Runtime Choice

The UI should detect runtimes already available on the user's computer, but runtime choice should not be the first thing the human has to think about.

Recommended default:

- Codex, when installed and available

Other runtimes can work if they can:

- run shell commands
- read and write local files
- parse JSON stdout
- inspect generated image artifacts

The kit should not require one specific runtime. Runtime choice can remain an advanced/debug detail until the launcher directly starts runtimes.

## Runtime Detection

The first implementation uses a small launcher that checks for common local agent CLIs on `PATH`.

Suggested detection order:

- `codex`
- `claude`
- `cursor`

If Codex is found, make it the recommended default. If no known runtime is found, prompt the user to install one or enter a custom command only after the two-step input flow has captured the source and target.

The launcher should pass the repo path and generated `agent-brief.md` to the selected runtime. It should not require a runtime-specific SDK.

## UI Inputs

Minimum user inputs:

- Step 1: `source_url`, one URL that contains or references the visual style assets
- Step 2: `target`, the first object or image to produce

Optional advanced/debug inputs:

- runtime choice
- run name
- include/exclude asset filters
- max iteration count
- chroma-key color

## UI Outputs

The main UI should display:

- the primary generated candidate, when present
- otherwise `comparison.jpg`, `contact-sheet.jpg`, or a clear waiting state
- thumbnail candidate choices, when multiple candidates exist
- approve, refresh, and new-run controls

Debug details may display:

- collected reference assets
- `contact-sheet.jpg`
- current `prompt.txt`
- generated candidates
- transparent cutouts
- `comparison.jpg`
- `taste-notes.md`
- `agent-brief.md`
- current recommended next action

For v1, the bundled local browser UI displays the main artifacts directly. Native OS preview remains a valid fallback:

- macOS: `open <artifact-path>`
- Linux: `xdg-open <artifact-path>`
- Windows: `start <artifact-path>`

Richer browser orchestration can come later. The first human loop only needs a reliable way to show the latest generated or comparison artifact and ask: close enough, refresh, or start over?

## Approval And Locking

A style is locked when a human approves a candidate.

The launcher writes a `locked_style` block into `.style-kit-state.json`.

The locked context should include:

- source URL
- reference asset manifest
- contact sheet
- accepted candidate
- prompt that produced it
- taste notes
- state file snapshot
- chroma-key settings
- maximum iteration rule

Example state shape:

```json
{
  "locked_style": {
    "source_url": "https://example.com",
    "contact_sheet": "outputs/runs/mango/contact-sheet.jpg",
    "accepted_candidate": "outputs/runs/mango/cutouts/mango-alpha.png",
    "prompt": "outputs/runs/mango/prompt.txt",
    "taste_notes": "outputs/runs/mango/taste-notes.md",
    "key_color": "ff00ff"
  }
}
```

After locking, the user can type a new target object or image request. The agent should reuse the locked style context instead of rediscovering the source URL from scratch.

The later command shape can be:

```bash
style-kit generate-locked "watermelon slice"
```

That command should read `locked_style`, create a new run, and preserve the same reference sheet and prompt constraints.

## Current Limits

The first UI pass does not directly drive a long-running agent runtime yet. It prepares the run, shows the primary review artifact, keeps the generated agent brief and next action behind debug details, visualizes artifacts as they appear on disk, and persists approval. Runtime execution still happens through an agent reading `agent-brief.md`.

## Non-Goals For The CLI Kit

The current toolkit does not:

- ship a managed long-running agent
- require a specific agent runtime
- claim automatic taste scoring

Those are frontend/runtime orchestration responsibilities layered on top of this contract.
