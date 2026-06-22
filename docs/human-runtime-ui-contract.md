# Human Runtime UI Contract

The human-facing layer should be another consumer of the same agent-first toolkit contract.

It should not replace the CLI primitives. It should help a user run them through an installed local agent runtime and make each iteration visible.

## Intended Flow

```text
CLONE + RUN
  user clones the repo
  user starts the local launcher/UI

        v

PICK RUNTIME
  launcher detects installed agent CLIs on PATH
  recommended default: Codex
  user can choose another runtime

        v

ENTER URL + TARGET
  user enters one source URL
  user enters first target object or image request
  launcher calls prepare_agent_run.py

        v

ITERATE + SHOW
  agent runtime follows next_action.py
  each iteration writes artifacts to outputs/runs/<run>/
  launcher opens or displays the latest artifact
  user approves, rejects, or writes correction text

        v

STYLE LOCKED
  approved run is persisted as locked_style
  future targets reuse locked_style without rediscovering the URL
```

## Runtime Choice

The UI should let the user choose a runtime already available on their computer.

Recommended default:

- Codex, when installed and available

Other runtimes can work if they can:

- run shell commands
- read and write local files
- parse JSON stdout
- inspect generated image artifacts

The kit should not require one specific runtime.

## Runtime Detection

The first implementation can use a small launcher that checks for common local agent CLIs on `PATH`.

Suggested detection order:

- `codex`
- `claude`
- `cursor`

If Codex is found, make it the recommended default. If no known runtime is found, prompt the user to install one or enter a custom command.

The launcher should pass the repo path and generated `agent-brief.md` to the selected runtime. It should not require a runtime-specific SDK.

## UI Inputs

Minimum user inputs:

- `source_url`: one URL that contains or references the visual style assets
- `target`: the first object or image to produce

Optional user inputs:

- runtime choice
- run name
- include/exclude asset filters
- max iteration count
- chroma-key color

## UI Outputs

The UI should display:

- collected reference assets
- `contact-sheet.jpg`
- current `prompt.txt`
- generated candidates
- transparent cutouts
- `comparison.jpg`
- `taste-notes.md`
- current recommended next action

For v1, native OS preview is enough:

- macOS: `open <artifact-path>`
- Linux: `xdg-open <artifact-path>`
- Windows: `start <artifact-path>`

A dedicated browser frontend can come later. The first human loop only needs a reliable way to show the latest generated or comparison artifact and ask: close enough, retry, or add correction?

## Approval And Locking

A style is locked when a human approves a candidate.

The launcher should write a `locked_style` block into `.style-kit-state.json`.

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

## Non-Goals For The CLI Kit

The current toolkit does not:

- ship a managed long-running agent
- require a specific agent runtime
- provide a web server
- provide a visual approval UI
- claim automatic taste scoring

Those are frontend/runtime orchestration responsibilities layered on top of this contract.
