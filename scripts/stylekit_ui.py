#!/usr/bin/env python3
"""Run a local visual UI for artifact-style-kit.

The UI is deliberately thin: it calls the existing CLI scripts, reads the same
.style-kit-state.json file, and displays the resulting run artifacts.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import subprocess
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = REPO_ROOT / ".style-kit-state.json"
RUNTIME_ORDER = ["codex", "claude", "cursor"]


def nowish_error(message: str, status: int = 400) -> tuple[int, dict[str, Any]]:
    return status, {"status": "error", "error": message}


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def repo_path(value: str | None) -> Path:
    if not value:
        raise ValueError("missing path")
    candidate = (REPO_ROOT / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    if candidate != REPO_ROOT and REPO_ROOT not in candidate.parents:
        raise ValueError("path is outside this repository")
    return candidate


def rel(path: str | Path | None) -> str | None:
    if not path:
        return None
    resolved = repo_path(str(path))
    return str(resolved.relative_to(REPO_ROOT))


def detect_runtimes() -> list[dict[str, Any]]:
    runtimes: list[dict[str, Any]] = []
    for name in RUNTIME_ORDER:
        binary = shutil.which(name)
        runtimes.append(
            {
                "name": name,
                "available": bool(binary),
                "path": binary,
                "recommended": name == "codex",
            }
        )
    return runtimes


def image_files(value: str | None) -> list[dict[str, str]]:
    if not value:
        return []
    root = repo_path(value)
    if not root.exists():
        return []
    paths = sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
    )
    return [{"name": item.name, "path": rel(item)} for item in paths]


def run_next_action() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "scripts/next_action.py", "--json"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "status": "error",
            "error": proc.stderr.strip() or proc.stdout.strip() or "next_action.py failed",
        }
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": proc.stdout.strip()}


def current_view() -> dict[str, Any]:
    state = load_json(STATE_PATH)
    current_run = state.get("current_run")
    run = state.get("runs", {}).get(current_run, {}) if current_run else {}
    files = run.get("files", {})
    artifacts = {
        "reference_assets": image_files(files.get("reference_dir")),
        "generated": image_files(files.get("generated_dir")),
        "cutouts": image_files(files.get("cutouts_dir")),
    }
    key_files = {
        key: rel(files.get(key))
        for key in ["contact_sheet", "comparison", "prompt", "taste_notes", "agent_brief"]
        if files.get(key) and repo_path(files.get(key)).exists()
    }
    return {
        "status": "ok",
        "repo": str(REPO_ROOT),
        "runtimes": detect_runtimes(),
        "state": state,
        "current_run": current_run,
        "run": run,
        "files": key_files,
        "artifacts": artifacts,
        "next_action": run_next_action(),
    }


def prepare_run(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    subject = str(payload.get("subject", "")).strip()
    source_url = str(payload.get("source_url", "")).strip()
    run_name = str(payload.get("run_name", "")).strip()
    include = [str(item).strip() for item in payload.get("include", []) if str(item).strip()]
    exclude = [str(item).strip() for item in payload.get("exclude", []) if str(item).strip()]
    max_iterations = int(payload.get("max_iterations") or 5)

    if not subject:
        return nowish_error("target is required")
    if not source_url:
        return nowish_error("source_url is required")
    if max_iterations < 1 or max_iterations > 20:
        return nowish_error("max_iterations must be between 1 and 20")

    cmd = [
        sys.executable,
        "scripts/prepare_agent_run.py",
        "--subject",
        subject,
        "--source-url",
        source_url,
        "--max-iterations",
        str(max_iterations),
    ]
    if run_name:
        cmd.extend(["--run-name", run_name])
    for pattern in include:
        cmd.extend(["--include", pattern])
    for pattern in exclude:
        cmd.extend(["--exclude", pattern])

    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        return 500, {
            "status": "error",
            "error": proc.stderr.strip() or proc.stdout.strip() or "prepare_agent_run.py failed",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {"status": "ok", "raw": proc.stdout}
    return 200, {"status": "ok", "prepare": data, "view": current_view()}


def lock_style(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    candidate = str(payload.get("candidate_path", "")).strip()
    if not candidate:
        return nowish_error("candidate_path is required")
    candidate_path = repo_path(candidate)
    if not candidate_path.exists():
        return nowish_error("candidate_path does not exist", 404)

    state = load_json(STATE_PATH)
    current_run = state.get("current_run")
    run = state.get("runs", {}).get(current_run, {}) if current_run else {}
    if not run:
        return nowish_error("no current run to lock")
    files = run.get("files", {})
    locked = {
        "source_url": run.get("source_url"),
        "subject": run.get("subject"),
        "run_name": current_run,
        "contact_sheet": rel(files.get("contact_sheet")),
        "accepted_candidate": rel(candidate_path),
        "prompt": rel(files.get("prompt")),
        "taste_notes": rel(files.get("taste_notes")),
        "reference_manifest": rel(files.get("assets_manifest")) if files.get("assets_manifest") else None,
        "key_color": str(payload.get("key_color") or "ff00ff"),
        "locked_by": "stylekit_ui",
    }
    state["locked_style"] = locked
    state.setdefault("runs", {}).setdefault(current_run, {}).update({"status": "locked", "accepted_candidate": rel(candidate_path)})
    save_json(STATE_PATH, state)
    return 200, {"status": "ok", "locked_style": locked, "view": current_view()}


def text_payload(path_value: str | None) -> tuple[int, dict[str, Any]]:
    try:
        path = repo_path(path_value)
    except ValueError as exc:
        return nowish_error(str(exc))
    if not path.exists():
        return nowish_error("file does not exist", 404)
    return 200, {"status": "ok", "path": rel(path), "text": path.read_text(errors="replace")}


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>artifact-style-kit</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #171f1c;
      --muted: #69736f;
      --line: #d9ded8;
      --paper: #f7f7f2;
      --panel: #ffffff;
      --soft: #eef1ec;
      --ok: #226c4b;
      --bad: #a43434;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: var(--paper); color: var(--ink); }
    button, input, textarea { font: inherit; }
    .app { width: min(920px, calc(100vw - 32px)); margin: 0 auto; padding: 38px 0 56px; }
    header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 34px; }
    h1 { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: 0; }
    .step-count { color: var(--muted); font-size: 13px; }
    .screen { display: none; }
    .screen.active { display: block; }
    .prompt { max-width: 720px; margin: 0 auto; display: grid; gap: 18px; }
    .prompt h2 { margin: 0; font-size: clamp(34px, 6vw, 68px); line-height: 1.02; font-weight: 620; letter-spacing: 0; }
    .field { display: grid; gap: 10px; }
    label { color: var(--muted); font-size: 13px; }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      color: var(--ink);
      padding: 16px 17px;
      font-size: 18px;
      outline: none;
      box-shadow: 0 1px 0 rgba(0,0,0,.02);
    }
    textarea { min-height: 112px; resize: vertical; }
    input:focus, textarea:focus { border-color: #aeb8b2; box-shadow: 0 0 0 3px rgba(23,31,28,.06); }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    button {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: white;
      color: var(--ink);
      min-height: 44px;
      padding: 0 16px;
      cursor: pointer;
    }
    button.primary { background: var(--ink); border-color: var(--ink); color: white; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .message { min-height: 22px; color: var(--muted); font-size: 13px; }
    .message.error { color: var(--bad); }
    .review { display: grid; gap: 18px; }
    .review-head { display: flex; justify-content: space-between; align-items: end; gap: 16px; flex-wrap: wrap; }
    .review-head h2 { margin: 0; font-size: 24px; line-height: 1.15; }
    .artifact-main {
      border: 1px solid var(--line);
      background: white;
      border-radius: 8px;
      min-height: 520px;
      display: grid;
      place-items: center;
      overflow: hidden;
    }
    .artifact-main img { width: 100%; max-height: 76vh; object-fit: contain; display: block; background: var(--soft); }
    .empty {
      width: min(520px, 100%);
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 28px;
      color: var(--muted);
      line-height: 1.45;
      background: #fbfbf7;
      text-align: center;
    }
    .thumbs { display: flex; gap: 10px; flex-wrap: wrap; }
    .thumb {
      width: 96px;
      height: 76px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: white;
      padding: 0;
      overflow: hidden;
    }
    .thumb.selected { border-color: var(--ink); box-shadow: 0 0 0 2px var(--ink); }
    .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
    details {
      border-top: 1px solid var(--line);
      padding-top: 16px;
      color: var(--muted);
      font-size: 13px;
    }
    summary { cursor: pointer; color: var(--ink); margin-bottom: 12px; }
    .debug { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .debug-box { border: 1px solid var(--line); border-radius: 8px; background: white; padding: 12px; min-width: 0; }
    .debug-box h3 { margin: 0 0 8px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0; }
    pre {
      margin: 0;
      max-height: 280px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: var(--ink);
    }
    a { color: inherit; }
    @media (max-width: 700px) {
      .app { width: min(100vw - 24px, 920px); padding-top: 24px; }
      header { margin-bottom: 24px; }
      .artifact-main { min-height: 360px; }
      .prompt h2 { font-size: 38px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <h1>artifact-style-kit</h1>
      <div id="stepCount" class="step-count">Step 1 of 2</div>
    </header>

    <section id="screenSource" class="screen active">
      <div class="prompt">
        <h2>Paste the style source.</h2>
        <div class="field">
          <label for="sourceUrl">Source URL</label>
          <input id="sourceUrl" placeholder="https://example.com/style-source" autocomplete="off" autofocus>
        </div>
        <div class="actions">
          <button id="toTarget" class="primary">Next</button>
        </div>
        <div id="messageSource" class="message"></div>
      </div>
    </section>

    <section id="screenTarget" class="screen">
      <div class="prompt">
        <h2>Describe what to make.</h2>
        <div class="field">
          <label for="subject">Target</label>
          <textarea id="subject" placeholder="one small object or image request"></textarea>
        </div>
        <div class="actions">
          <button id="backToSource">Back</button>
          <button id="prepare" class="primary">Start</button>
        </div>
        <div id="messageTarget" class="message"></div>
      </div>
    </section>

    <section id="screenReview" class="screen">
      <div class="review">
        <div class="review-head">
          <div>
            <div id="runMeta" class="step-count"></div>
            <h2 id="reviewTitle">Review the result.</h2>
          </div>
          <div class="actions">
            <button id="newRun">New run</button>
            <button id="refresh">Refresh</button>
            <button id="approve" class="primary">Looks right</button>
          </div>
        </div>
        <div id="artifactMain" class="artifact-main"></div>
        <div id="thumbs" class="thumbs"></div>
        <div id="messageReview" class="message"></div>

        <details>
          <summary>Debug details</summary>
          <div class="debug">
            <div class="debug-box">
              <h3>Next action</h3>
              <pre id="nextAction"></pre>
            </div>
            <div class="debug-box">
              <h3>Agent brief</h3>
              <pre id="agentBrief"></pre>
            </div>
            <div class="debug-box">
              <h3>Contact sheet</h3>
              <div id="contactSheet"></div>
            </div>
          </div>
        </details>
      </div>
    </section>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let view = null;
    let selectedCandidate = null;
    let pendingSource = '';

    function fileUrl(path) {
      return `/api/file?path=${encodeURIComponent(path)}`;
    }

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: {'content-type': 'application/json'},
        ...options
      });
      const data = await res.json();
      if (!res.ok || data.status === 'error') throw new Error(data.error || 'request failed');
      return data;
    }

    function screen(name) {
      ['Source', 'Target', 'Review'].forEach((item) => {
        $(`screen${item}`).classList.toggle('active', item === name);
      });
      $('stepCount').textContent = name === 'Source' ? 'Step 1 of 2' : name === 'Target' ? 'Step 2 of 2' : 'Review';
    }

    function setMessage(id, text, isError = false) {
      $(id).textContent = text || '';
      $(id).classList.toggle('error', Boolean(isError));
    }

    function candidates() {
      return [...(view?.artifacts?.cutouts || []), ...(view?.artifacts?.generated || [])];
    }

    function selectedArtifact() {
      const items = candidates();
      return selectedCandidate || items[0]?.path || null;
    }

    async function loadText(path) {
      if (!path) return 'No file yet.';
      const data = await api(`/api/text?path=${encodeURIComponent(path)}`);
      return data.text;
    }

    async function refresh() {
      view = await api('/api/state');
      const run = view.run || {};
      $('runMeta').textContent = view.current_run ? `${view.current_run} · ${run.status || 'prepared'}` : 'No run yet';
      selectedCandidate = selectedCandidate || candidates()[0]?.path || null;
      renderMainArtifact();
      renderThumbs();
      await renderDebug();
    }

    function renderMainArtifact() {
      const path = selectedArtifact();
      $('approve').disabled = !path;
      $('approve').textContent = path ? 'Looks right' : 'Waiting for candidate';
      if (!path) {
        $('artifactMain').innerHTML = '<div class="empty">Run prepared. Generate a candidate, then refresh.</div>';
        return;
      }
      $('artifactMain').innerHTML = `<a href="${fileUrl(path)}" target="_blank"><img src="${fileUrl(path)}" alt="${path}"></a>`;
    }

    function renderThumbs() {
      const items = candidates();
      if (!items.length) {
        $('thumbs').innerHTML = '';
        return;
      }
      $('thumbs').innerHTML = items.map((item) => `
        <button class="thumb ${item.path === selectedCandidate ? 'selected' : ''}" data-candidate="${item.path}" title="${item.name}">
          <img src="${fileUrl(item.path)}" alt="${item.name}">
        </button>
      `).join('');
      document.querySelectorAll('[data-candidate]').forEach((button) => {
        button.onclick = () => {
          selectedCandidate = button.dataset.candidate;
          renderMainArtifact();
          renderThumbs();
        };
      });
    }

    async function renderDebug() {
      const next = view.next_action?.recommended_next?.[0];
      $('nextAction').textContent = next ? `${next.command}\n\n${next.why}` : JSON.stringify(view.next_action || {}, null, 2);
      $('agentBrief').textContent = await loadText(view.files?.agent_brief);
      $('contactSheet').innerHTML = view.files?.contact_sheet
        ? `<a href="${fileUrl(view.files.contact_sheet)}" target="_blank"><img src="${fileUrl(view.files.contact_sheet)}" style="width:100%;border-radius:6px;border:1px solid var(--line);"></a>`
        : 'No contact sheet yet.';
    }

    async function prepare() {
      const source = pendingSource.trim();
      const subject = $('subject').value.trim();
      if (!subject) {
        setMessage('messageTarget', 'Add one target prompt.', true);
        return;
      }
      $('prepare').disabled = true;
      setMessage('messageTarget', 'Preparing...');
      try {
        await api('/api/prepare', {
          method: 'POST',
          body: JSON.stringify({
            source_url: source,
            subject,
            max_iterations: 5
          })
        });
        selectedCandidate = null;
        await refresh();
        screen('Review');
        setMessage('messageReview', 'Run prepared. Refresh after the candidate lands.');
      } catch (error) {
        setMessage('messageTarget', error.message, true);
      } finally {
        $('prepare').disabled = false;
      }
    }

    async function lock() {
      if (!selectedCandidate) return;
      $('approve').disabled = true;
      try {
        await api('/api/lock', {
          method: 'POST',
          body: JSON.stringify({candidate_path: selectedCandidate})
        });
        await refresh();
        setMessage('messageReview', 'Style locked.');
      } catch (error) {
        setMessage('messageReview', error.message, true);
      } finally {
        $('approve').disabled = false;
      }
    }

    $('toTarget').onclick = () => {
      const value = $('sourceUrl').value.trim();
      if (!value) {
        setMessage('messageSource', 'Paste one source URL.', true);
        return;
      }
      pendingSource = value;
      setMessage('messageSource', '');
      screen('Target');
      $('subject').focus();
    };
    $('backToSource').onclick = () => screen('Source');
    $('newRun').onclick = () => screen('Source');
    $('prepare').onclick = prepare;
    $('refresh').onclick = () => refresh().catch((error) => setMessage('messageReview', error.message, true));
    $('approve').onclick = lock;

    refresh().catch(() => {});
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "stylekit-ui/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/state":
            self.send_json(200, current_view())
            return
        if parsed.path == "/api/text":
            status, payload = text_payload(query.get("path", [None])[0])
            self.send_json(status, payload)
            return
        if parsed.path == "/api/file":
            try:
                path = repo_path(query.get("path", [None])[0])
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            if not path.exists() or not path.is_file():
                self.send_error(404, "file does not exist")
                return
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("content-type", mime)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404, "not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            payload = self.read_json()
        except json.JSONDecodeError as exc:
            self.send_json(400, {"status": "error", "error": str(exc)})
            return
        if parsed.path == "/api/prepare":
            status, response = prepare_run(payload)
            self.send_json(status, response)
            return
        if parsed.path == "/api/lock":
            status, response = lock_style(payload)
            self.send_json(status, response)
            return
        self.send_json(404, {"status": "error", "error": "not found"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"stylekit UI: {url}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
