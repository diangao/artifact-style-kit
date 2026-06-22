# Taste Loop

The core loop is human-readable and reproducible:

1. Capture reference assets from a source surface.
2. Build a contact sheet that exposes shape, palette, viewpoint, texture, and object taxonomy.
3. Write a prompt recipe from visible constraints, not from hidden intent.
4. Generate candidates.
5. Compare candidates against the contact sheet.
6. Record what drifted and what improved.
7. Iterate with narrower constraints.

## Notes Template

```text
Iteration:
Reference:
Prompt file:
Generated outputs:

What matches:
- 

What drifts:
- 

Next constraints:
- 
```

## Useful Axes

- object taxonomy: what kinds of objects are allowed or forbidden
- viewpoint: front, side, isometric, three-quarter, top-down
- detail level: flat, low-poly, painterly, textured, photographic
- edge treatment: crisp, cutout, feathered, hand-trimmed
- palette: saturated, muted, pastel, monochrome, warm, cool
- background contract: transparent, flat key color, contextual scene
- export contract: PNG alpha, JPG contact sheet, manifest JSON

