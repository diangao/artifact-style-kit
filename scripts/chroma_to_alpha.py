#!/usr/bin/env python3
"""Remove a flat chroma-key background from an image."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def parse_hex(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError("--key must be a six-character hex color")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def channel_distance(pixel: tuple[int, int, int], key: tuple[int, int, int]) -> int:
    return max(abs(pixel[0] - key[0]), abs(pixel[1] - key[1]), abs(pixel[2] - key[2]))


def remove_key(input_path: Path, output_path: Path, key: tuple[int, int, int], threshold: int, feather: int) -> None:
    image = Image.open(input_path).convert("RGBA")
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            r, g, b, a = pixels[x, y]
            distance = channel_distance((r, g, b), key)
            if distance <= threshold:
                pixels[x, y] = (r, g, b, 0)
            elif feather and distance <= threshold + feather:
                alpha = int(255 * (distance - threshold) / feather)
                pixels[x, y] = (r, g, b, min(a, alpha))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key", default="ff00ff")
    parser.add_argument("--threshold", type=int, default=20)
    parser.add_argument("--feather", type=int, default=40)
    args = parser.parse_args()

    remove_key(args.input, args.output, parse_hex(args.key), args.threshold, args.feather)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

