# Agent-First Contract

This kit treats the agent as the primary user.

```text
INPUTS
  source_url               # one link for style reference discovery
  target.json              # subject, count, constraints, max_iterations
  .style-kit-state.json    # current iteration, previous outputs, drift notes

        v

STYLE KIT TOOLS
  collect_assets.py        -> reference-assets/ + assets.json
  build_contact_sheet.py   -> contact-sheet.jpg
  prepare_agent_run.py     -> prompt.txt + agent-brief.md + run.json
  chroma_to_alpha.py       -> transparent cutouts
  next_action.py           -> recommended next command

        v

OUTPUTS
  outputs/runs/<run>/
    reference-assets/
    assets.json
    contact-sheet.jpg
    prompt.txt
    generated/
    cutouts/
    comparison.jpg
    taste-notes.md
    run.json
    agent-brief.md

        v

AGENT LOOP
  inspect -> generate -> cutout -> compare -> write drift notes -> update state -> next_action
```

## Input Contract

Minimum inputs:

- `source_url`
- target subject

Optional inputs:

- run name
- target count
- include/exclude asset filters
- chroma-key color
- maximum iteration count, default `5`
- existing `.style-kit-state.json`

Manual fallback:

- reference images in a folder, used when URL collection is blocked, dynamic, or too noisy

## Output Contract

Every agent-facing tool should return or write:

- concrete artifact paths
- machine-readable state
- a recommended next action
- enough context for the agent to continue without chat history

The v1 completion gate is bounded:

- stop when a human approves the candidate
- otherwise stop after the configured maximum iteration count
- write remaining drift rather than looping indefinitely

## State Contract

The root `.style-kit-state.json` is the agent's continuity file.

It records:

- current run
- current iteration
- reference sheet path
- latest generated/cutout/comparison paths
- prompt path
- taste notes path
- recommended next action

The state file is intentionally lightweight. It is not a database and should remain easy for an agent to read in one pass.
