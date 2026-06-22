# Agent-First Contract

This kit treats the agent as the primary user.

```text
INPUTS
  reference_assets/        # images or captured source assets
  target.json              # subject, count, constraints
  .style-kit-state.json    # current iteration, previous outputs, drift notes

        v

STYLE KIT TOOLS
  collect_assets.py        -> assets.json
  build_contact_sheet.py   -> contact-sheet.jpg
  prepare_agent_run.py     -> prompt.txt + agent-brief.md + run.json
  chroma_to_alpha.py       -> transparent cutouts
  next_action.py           -> recommended next command

        v

OUTPUTS
  outputs/runs/<run>/
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

- reference images in a folder
- target subject
- run name

Optional inputs:

- target count
- negative constraints
- chroma-key color
- existing `.style-kit-state.json`

## Output Contract

Every agent-facing tool should return or write:

- concrete artifact paths
- machine-readable state
- a recommended next action
- enough context for the agent to continue without chat history

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

