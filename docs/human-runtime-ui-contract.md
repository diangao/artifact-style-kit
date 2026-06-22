# Human Runtime UI Contract

The human-facing layer should be another consumer of the same agent-first toolkit contract.

It should not replace the CLI primitives. It should help a user run them through an installed local agent runtime and make each iteration visible.

## Intended Flow

```text
USER
  chooses local agent runtime
  enters one source URL
  enters target object or image request

        v

LOCAL UI
  recommends Codex when available
  starts or guides the selected runtime in this repo
  calls prepare_agent_run.py with source_url + target
  watches outputs/runs/<run>/
  displays artifacts after each iteration

        v

AGENT RUNTIME
  reads AGENT.md and .style-kit-state.json
  calls next_action.py
  generates candidates
  converts cutouts
  builds comparisons
  writes taste-notes.md

        v

HUMAN REVIEW
  approve candidate
  or write correction text
  or continue up to max_iterations

        v

LOCKED STYLE CONTEXT
  approved run becomes reusable style context
  user can request more objects/images in the same style
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

## Approval And Locking

A style is locked when a human approves a candidate.

The locked context should include:

- source URL
- reference asset manifest
- contact sheet
- accepted candidate
- prompt that produced it
- taste notes
- state file snapshot

After locking, the user can type a new target object or image request. The agent should reuse the locked style context instead of rediscovering the source URL from scratch.

## Non-Goals For The CLI Kit

The current toolkit does not:

- ship a managed long-running agent
- require a specific agent runtime
- provide a web server
- provide a visual approval UI
- claim automatic taste scoring

Those are frontend/runtime orchestration responsibilities layered on top of this contract.
