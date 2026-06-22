#!/usr/bin/env python3
"""Collect image asset references from a URL or saved source files.

Tool contract:
- name: collect_assets
- purpose: find image references from a source URL or saved source files and optionally download them
- inputs: source URL or source files/directories, optional base URL, include/exclude filters
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
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.request import Request, urlopen, urlretrieve
from urllib import robotparser

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


def robots_allowed(source_url: str, user_agent: str) -> bool:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return True
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except Exception:
        return True
    return parser.can_fetch(user_agent, source_url)


def fetch_url(source_url: str, user_agent: str, timeout: float = 20.0) -> str:
    if not robots_allowed(source_url, user_agent):
        raise RuntimeError(f"robots.txt disallows fetching {source_url}")
    request = Request(source_url, headers={"User-Agent": user_agent})
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(content_type, errors="replace")


def iter_source_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*") if p.suffix.lower() in SOURCE_SUFFIXES))
        elif path.is_file():
            files.append(path)
    return files


def normalize_reference(raw: str) -> str:
    value = unquote(html.unescape(raw)).strip().rstrip(".,;:")
    parsed = urlparse(value)
    if parsed.path == "/_next/image":
        inner = parse_qs(parsed.query).get("url", [])
        if inner:
            return inner[0]
    return value


def compile_filters(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


def allowed(value: str, includes: list[re.Pattern[str]], excludes: list[re.Pattern[str]]) -> bool:
    if includes and not any(pattern.search(value) for pattern in includes):
        return False
    if excludes and any(pattern.search(value) for pattern in excludes):
        return False
    return True


def collect_from_text(
    text: str,
    source_label: str,
    base_url: str,
    asset_re: re.Pattern[str],
    includes: list[re.Pattern[str]],
    excludes: list[re.Pattern[str]],
) -> list[Asset]:
    seen: set[tuple[str, str]] = set()
    assets: list[Asset] = []
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
        assets.append(Asset(reference=reference, url=url, source_file=source_label))
    return assets


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
        for asset in collect_from_text(text, str(source_file), base_url, asset_re, includes, excludes):
            key = (asset.reference, asset.url)
            if key in seen:
                continue
            seen.add(key)
            assets.append(asset)
    return sorted(assets, key=lambda item: item.reference)


def collect_from_url(
    source_url: str,
    asset_re: re.Pattern[str],
    includes: list[re.Pattern[str]],
    excludes: list[re.Pattern[str]],
    user_agent: str,
) -> list[Asset]:
    text = fetch_url(source_url, user_agent)
    return sorted(
        collect_from_text(text, source_url, source_url, asset_re, includes, excludes),
        key=lambda item: item.reference,
    )


def safe_download_name(asset: Asset) -> Path:
    parsed = urlparse(asset.url)
    candidate = Path(parsed.path.lstrip("/"))
    if not candidate.name:
        candidate = Path(asset.reference.lstrip("/"))
    return candidate


def download_assets(assets: list[Asset], output_dir: Path) -> list[dict[str, str]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, str]] = []
    for asset in assets:
        destination = output_dir / safe_download_name(asset)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            urlretrieve(asset.url, destination)
        except Exception as exc:
            errors.append({"url": asset.url, "error": str(exc)})
    return errors


def write_manifest(assets: list[Asset], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps([asdict(asset) for asset in assets], indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", type=Path)
    parser.add_argument("--source-url", help="Fetch one URL and collect image references from its HTML.")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--asset-regex", default=DEFAULT_ASSET_RE.pattern)
    parser.add_argument("--include", action="append", default=[], help="Regex filter. May be repeated.")
    parser.add_argument("--exclude", action="append", default=[], help="Regex exclusion. May be repeated.")
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--user-agent", default="artifact-style-kit/1.0")
    parser.add_argument("--json", action="store_true", help="Print an agent-readable JSON response.")
    args = parser.parse_args()

    if not args.source_url and not args.sources:
        parser.error("provide --source-url or at least one saved source path")

    try:
        asset_re = re.compile(args.asset_regex, re.IGNORECASE)
        includes = compile_filters(args.include)
        excludes = compile_filters(args.exclude)
    except re.error as exc:
        parser.error(str(exc))

    if args.source_url:
        assets = collect_from_url(args.source_url, asset_re, includes, excludes, args.user_agent)
    else:
        assets = collect(args.sources, args.base_url, asset_re, includes, excludes)
    if args.manifest:
        write_manifest(assets, args.manifest)
    download_errors: list[dict[str, str]] = []
    if args.download_dir:
        download_errors = download_assets(assets, args.download_dir)

    if args.json:
        emit_json(
            ok_payload(
                {
                    "source_url": args.source_url,
                    "asset_count": len(assets),
                    "assets": [asdict(asset) for asset in assets],
                    "manifest": str(args.manifest) if args.manifest else None,
                    "download_dir": str(args.download_dir) if args.download_dir else None,
                    "download_error_count": len(download_errors),
                    "download_errors": download_errors,
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
