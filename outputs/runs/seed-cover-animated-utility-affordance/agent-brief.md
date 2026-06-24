# Agent Brief

Subject: cover thumbnail: one ripe mango with a small green leaf rendered as Animated utility affordance
Source URL: manual reference folder
Stop rule: stop after 1 iterations or when a human approves a candidate.

## Inspect First

1. Open `outputs/runs/seed-cover-animated-utility-affordance/contact-sheet.jpg`.
2. Read `outputs/runs/seed-cover-animated-utility-affordance/source-review.json`.
3. If the source review is still `draft`, stop for human extraction review.
4. Use the confirmed elements; ignore `ignored_reference_assets` and account for `missing_reference_notes`.
5. Read `outputs/runs/seed-cover-animated-utility-affordance/prompt.txt`.
6. Generate candidate assets using the prompt.
7. Save generated chroma-key images in `outputs/runs/seed-cover-animated-utility-affordance/generated`.

## After Generation

Convert each chroma-key result to alpha:

```bash
python3 scripts/chroma_to_alpha.py \
  --input outputs/runs/seed-cover-animated-utility-affordance/generated/<candidate>.png \
  --output outputs/runs/seed-cover-animated-utility-affordance/cutouts/<candidate>-alpha.png \
  --key ff00ff
```

Build a comparison sheet:

```bash
python3 scripts/build_contact_sheet.py \
  --input-dir outputs/runs/seed-cover-animated-utility-affordance/cutouts \
  --output outputs/runs/seed-cover-animated-utility-affordance/comparison.jpg \
  --columns 4 \
  --labels
```

Then update `outputs/runs/seed-cover-animated-utility-affordance/taste-notes.md` with:

- what matches
- what drifts
- the next prompt constraint

## Judgment Rule

Prefer visible similarity to the reference sheet over generic polish. If a candidate is prettier but less aligned, mark the drift explicitly.

Do not treat the raw contact sheet as the whole spec. The source review chooses which extracted elements are valid evidence.

Do not loop forever. If the candidate still drifts after 1 iterations, save the best candidate and flag the remaining drift for human review.
