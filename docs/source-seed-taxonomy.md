# Source Seed Taxonomy

These are not asset libraries first. They are reusable visual-treatment
contracts: a source page should give the agent a concrete object grammar,
material rule, layout rule, or interaction rule that can survive a new target.

## Current Treatment Buckets

| Seed | Canonical source | Reusable contract |
| --- | --- | --- |
| Faceted object cutouts | human-supplied source URL | Convert a subject into a transparent, faceted, hand-painted object cutout. |
| Dither card system | `https://contra.com/community/pHXPsYuS-transform-your-designs-mastering-dither-ascii` | Use low-ink dither / ASCII texture as a repeatable graphic system, usually on cards or animated surfaces. |
| Disco-shell icon | `https://www.racejohnson.com/projects/discomorphism` | Turn a logo or simple object into a hard mirrored app-icon shell with square disco facets. |
| Prismatic glass object | `https://x.com/poletaeviktor/status/2069484424844960190` | Stage one emblem as a refractive glass object on a dark background. |
| Whimsical digital room | `https://www.aileenis.online/` | Treat a page or identity as a small inhabited room made of windows, props, and soft personal scenes. |
| Physical-metaphor UI | `https://ryanstephen.co/` | Translate a digital action into an analog object: clock, paper, folder, grass, or spatial pane. |
| Animated utility affordance | `https://lab01.dev/#ui-experiment` | Rebuild a utility control with exact icon, font, color-token, and motion constraints. |
| Object constellation UI | `https://feather.computer/` | Represent information as sparse floating object clusters on a canvas instead of a list. |

## Seed Cover Contract

The seed shelf covers are generated artifacts, not generic icons. Each cover
uses the same dog object target from `examples/seed-covers/dog-object-source.svg`,
so the shelf compares style treatment instead of subject matter. The dog
geometry must stay original: do not redraw, squash, extra-round, or mascot-ify
the supplied object. The previous mango shelf is preserved as checkpoint commit
`9c8e351`.
Seed-cover candidates should be rejected if they merely render a generic object
in a broad aesthetic; they need original-object preservation plus a clear
likeness to the source treatment's material, edge, lighting, and layout logic.

Cover provenance is recorded in `examples/seed-covers/cover-manifest.json`.
Each entry points to a `outputs/runs/seed-cover-*` run with:

- reference evidence copied into `reference-assets/`
- a source `contact-sheet.jpg`
- confirmed `source-review.json`
- `prompt.txt`, `taste-notes.md`, and `run.json`
- a generated dog-object cover candidate in `generated/`


## Selection Rule

A seed earns a slot when it has at least two of these:

- a visible object grammar, not only a moodboard
- a repeatable input/output transformation
- source-page evidence the extraction pass can inspect
- a named or easily nameable treatment
- implementation clues such as icons, fonts, colors, CSS, source links, or prompts
