# Taste Notes

Subject: cover thumbnail: the original compact gray dog SVG object with unchanged silhouette, angled closed eyes, black muzzle, rounded rectangular body, and small top-left handle/ear rendered as Physical-metaphor UI
Source URL: manual/reference evidence folder
Max iterations: 1

## Human Feedback Driving This Pass

- The prior dog sheet changed the object identity; the dog should stay the original dog from the supplied SVG.
- The active shelf now preserves the original dog geometry and changes only treatment/material/context across seed styles.
- The previous mango shelf remains preserved at commit `9c8e351`.

## What Matches

- Keeps the same original dog object across the seed shelf, so users compare style rather than subject.
- Preserves the supplied SVG's silhouette, angled closed eyes, black muzzle, rounded body, and top-left handle/ear.
- Quotes the selected source treatment through material, texture, lighting, and surrounding context without redesigning the dog.

## What Still Drifts

- This is still a seed-cover artifact, not a fully locked production style.
- The cover is cropped from a shared 4x2 rendered grid, so final production runs should still use the run's own source review and reference assets.

## Next Prompt Constraints

- Original dog geometry is mandatory: no redrawing, squashing, extra rounding, or mascot redesign.
- Similarity is mandatory: borrow the source treatment's material, edge language, and layout, not just a broad aesthetic label.
- The treatment can alter fill, texture, lighting, context, and UI/background environment only.

## Decision

- [x] keep as original-dog seed cover candidate
- [ ] revise
- [ ] discard
