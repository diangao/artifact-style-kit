#!/usr/bin/env python3
"""Build a visual contact sheet from a folder of image assets.

Tool contract:
- name: build_contact_sheet
- purpose: turn a folder of image assets into one glanceable reference or comparison sheet
- inputs: image directory, output path, layout options
- outputs: contact-sheet image
- typical next tool: prepare_agent_run.py for references, or taste-note update for generated cutouts
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from stylekit_common import emit_json, ok_payload


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def image_paths(input_dir: Path) -> list[Path]:
    return sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def load_font(size: int) -> ImageFont.ImageFont:
    for font_name in ("Arial.ttf", "Helvetica.ttf"):
        try:
            return ImageFont.truetype(font_name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def fit_image(path: Path, size: int) -> Image.Image:
    image = Image.open(path).convert("RGBA")
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset = ((size - image.width) // 2, (size - image.height) // 2)
    canvas.alpha_composite(image, offset)
    return canvas


def parse_color(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError("color must be six hex digits")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def build_sheet(
    paths: list[Path],
    output: Path,
    cell_size: int,
    columns: int,
    label_height: int,
    background: tuple[int, int, int],
    labels: bool,
    base_dir: Path,
) -> None:
    if not paths:
        raise ValueError(f"no images found in {base_dir}")

    rows = math.ceil(len(paths) / columns)
    width = columns * cell_size
    height = rows * (cell_size + (label_height if labels else 0))
    sheet = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(sheet)
    font = load_font(12)

    for index, path in enumerate(paths):
        row, column = divmod(index, columns)
        x = column * cell_size
        y = row * (cell_size + (label_height if labels else 0))
        asset = fit_image(path, int(cell_size * 0.82))
        sheet.paste(asset, (x + (cell_size - asset.width) // 2, y + 4), asset)
        if labels:
            label = str(path.relative_to(base_dir))
            draw.text((x + 6, y + cell_size - 6), label[:32], fill=(50, 50, 45), font=font, anchor="ls")

    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, quality=92)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cell-size", type=int, default=160)
    parser.add_argument("--columns", type=int, default=8)
    parser.add_argument("--label-height", type=int, default=24)
    parser.add_argument("--background", default="e8e9e0")
    parser.add_argument("--labels", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print an agent-readable JSON response.")
    args = parser.parse_args()

    paths = image_paths(args.input_dir)
    build_sheet(
        paths=paths,
        output=args.output,
        cell_size=args.cell_size,
        columns=args.columns,
        label_height=args.label_height,
        background=parse_color(args.background),
        labels=args.labels,
        base_dir=args.input_dir,
    )
    if args.json:
        emit_json(
            ok_payload(
                {
                    "input_dir": str(args.input_dir),
                    "output": str(args.output),
                    "image_count": len(paths),
                },
                [
                    {
                        "command": "Open the contact sheet and inspect style constraints.",
                        "why": "The next decision depends on visual alignment, not only file existence.",
                    }
                ],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
