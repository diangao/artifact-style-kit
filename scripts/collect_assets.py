#!/usr/bin/env python3
"""Collect image asset references from saved source files.

Tool contract:
- name: collect_assets
- purpose: find image references in saved source files and optionally download them
- inputs: source files/directories, optional base URL, include/exclude filters
- outputs: asset URLs, optional manifest JSON, optional downloaded files
- typical next tool: build_contact_sheet.py
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import urlretrieve

from stylekit_common import emit_json, ok_payload


DEFAULT_ASSET_RE = re.compile(
    r"(?P<asset>(?:https?://[^'\"()\s]+|/[^'\"()\s]+|[A-Za-z0-9_./-]+)"
    r"\.(?:png|jpg|jpeg|webp|gif|svg))",
    re.IGNORECASE,
)

SOURCE_SUFFIXES = {".html", ".htm", ".css", ".js", ".json", ".txt", ".md"}


@dataclass(frozen=True)
class Asset:
    reference: str
    url: str
    source_file: str


def iter_source_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in SOURCE_SUFFIXES))
        elif path.is_file():
            files.append(path)
    return files


def normalize_reference(raw: str) -> str:
    return unquote(html.unescape(raw)).strip().rstrip(".,;:")


def compile_filters(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def allowed(value: str, includes: list[re.Pattern[str]], excludes: list[re.Pattern[str]]) -> bool:
    if includes and not any(pattern.search(value) for pattern in includes):
        return False
    if excludes and any(pattern.search(value) for pattern in excludes):
        return False
    return True


def collect(
    sources: list[Path],
    base_url: str,
    asset_re: re.Pattern[str],
    includes: list[re.Pattern[str]],
    excludes: list[re.Pattern[str]],
) -> list[Asset]:
    seen: set[tuple[str, str]] = set()
    assets: list[Asset] = []
    for source_file in iter_source_files(sources):
        text = source_file.read_text(errors="ignore")
        decoded = unquote(html.unescape(text))
        for match in asset_re.finditer(decoded):
            reference = normalize_reference(match.group("asset") if "asset" in match.groupdict() else match.group(0))
            if not allowed(reference, includes, excludes):
                continue
            url = urljoin(base_url, reference) if base_url else reference
            key = (reference, url)
            if key in seen:
                continue
            seen.add(key)
            assets.append(Asset(reference=reference, url=url, source_file=str(source_file)))
    return sorted(assets, key=lambda item: item.reference)


def safe_download_name(asset: Asset) -> Path:
    parsed = urlparse(asset.url)
    candidate = Path(parsed.path.lstrip("/"))
    if not candidate.name:
        candidate = Path(asset.reference.lstrip("/"))
    return candidate


def download_assets(assets: list[Asset], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        destination = output_dir / safe_download_name(asset)
        destination.parent.mkdir(parents=True, exist_ok=True)
        urlretrieve(asset.url, destination)


def write_manifest(assets: list[Asset], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps([asdict(asset) for asset in assets], indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--asset-regex", default=DEFAULT_ASSET_RE.pattern)
    parser.add_argument("--include", action="append", default=[], help="Regex filter. May be repeated.")
    parser.add_argument("--exclude", action="append", default=[], help="Regex exclusion. May be repeated.")
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--json", action="store_true", help="Print an agent-readable JSON response.")
    args = parser.parse_args()

    try:
        asset_re = re.compile(args.asset_regex, re.IGNORECASE)
        includes = compile_filters(args.include)
        excludes = compile_filters(args.exclude)
    except re.error as exc:
        parser.error(str(exc))

    assets = collect(args.sources, args.base_url, asset_re, includes, excludes)
    if args.manifest:
        write_manifest(assets, args.manifest)
    if args.download_dir:
        download_assets(assets, args.download_dir)

    if args.json:
        emit_json(
            ok_payload(
                {
                    "asset_count": len(assets),
                    "assets": [asdict(asset) for asset in assets],
                    "manifest": str(args.manifest) if args.manifest else None,
                    "download_dir": str(args.download_dir) if args.download_dir else None,
                },
                [
                    {
                        "command": "python3 scripts/build_contact_sheet.py --input-dir <asset-dir> --output outputs/contact-sheet.jpg --labels --json",
                        "why": "Build a visual reference sheet from the collected assets.",
                    }
                ],
            )
        )
    else:
        for asset in assets:
            print(asset.url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
