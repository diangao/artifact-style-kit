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
      --ink: #18221f;
      --muted: #66736f;
      --line: #d9ded8;
      --paper: #f7f7f2;
      --panel: #ffffff;
      --soft: #edf0ea;
      --accent: #0f6f68;
      --accent-2: #9a4d2f;
      --ok: #1f7a4d;
      --warn: #a75f16;
      --bad: #a43434;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--paper); color: var(--ink); }
    button, input, textarea, select { font: inherit; }
    .shell { min-height: 100vh; display: grid; grid-template-columns: minmax(320px, 420px) minmax(0, 1fr); }
    aside { border-right: 1px solid var(--line); background: #fbfbf7; padding: 20px; display: flex; flex-direction: column; gap: 18px; }
    main { padding: 20px; display: grid; gap: 18px; align-content: start; }
    h1 { margin: 0; font-size: 20px; letter-spacing: 0; }
    h2 { margin: 0 0 10px; font-size: 13px; text-transform: uppercase; letter-spacing: 0; color: var(--muted); }
    label { display: grid; gap: 6px; font-size: 12px; color: var(--muted); }
    input, textarea { width: 100%; border: 1px solid var(--line); background: white; color: var(--ink); border-radius: 6px; padding: 10px 11px; min-height: 40px; }
    textarea { min-height: 70px; resize: vertical; }
    button { border: 1px solid var(--line); background: white; color: var(--ink); border-radius: 6px; padding: 9px 11px; cursor: pointer; }
    button.primary { background: var(--ink); color: white; border-color: var(--ink); }
    button:disabled { opacity: .5; cursor: not-allowed; }
    .stack { display: grid; gap: 10px; }
    .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .runtime { display: flex; flex-wrap: wrap; gap: 8px; }
    .runtime button { padding: 7px 9px; }
    .runtime button.selected { border-color: var(--accent); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
    .runtime button.unavailable { color: var(--muted); background: var(--soft); }
    .status { border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 12px; display: grid; gap: 8px; }
    .pill { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line); border-radius: 999px; padding: 5px 8px; color: var(--muted); background: white; font-size: 12px; }
    .pill.ok { color: var(--ok); border-color: #bed8ca; }
    .pill.warn { color: var(--warn); border-color: #ead2b5; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }
    .panel { border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 14px; min-width: 0; }
    .artifact { border: 1px solid var(--line); background: #fdfdf9; border-radius: 8px; overflow: hidden; display: grid; gap: 8px; }
    .artifact img { width: 100%; aspect-ratio: 4 / 3; object-fit: contain; background: #eef1ec; display: block; }
    .artifact footer { padding: 0 10px 10px; display: flex; justify-content: space-between; gap: 8px; align-items: center; }
    .artifact code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    pre { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; background: #f1f2ec; border: 1px solid var(--line); border-radius: 7px; padding: 12px; max-height: 420px; overflow: auto; }
    .hero-img { width: 100%; max-height: 360px; object-fit: contain; border: 1px solid var(--line); border-radius: 8px; background: #eef1ec; }
    .tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
    .tabs button.active { border-color: var(--accent); color: var(--accent); }
    .empty { color: var(--muted); border: 1px dashed var(--line); border-radius: 8px; padding: 16px; background: #fbfbf7; }
    .error { border-color: #e0b4b4; background: #fff7f7; color: var(--bad); }
    .fine { color: var(--muted); font-size: 12px; line-height: 1.45; }
    a { color: var(--accent); text-decoration: none; }
    @media (max-width: 900px) {
      .shell { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      main { padding: 14px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <header class="stack">
        <h1>artifact-style-kit</h1>
        <div class="fine">One style URL in, visible artifact loop out. The UI stays on the same CLI + JSON + filesystem contract.</div>
      </header>

      <section class="stack">
        <h2>Runtime</h2>
        <div id="runtimes" class="runtime"></div>
        <div class="fine">Codex is recommended when available. Runtime launching is still agent-managed; this UI prepares and visualizes the loop.</div>
      </section>

      <section class="stack">
        <h2>New Run</h2>
        <label>Source URL <input id="sourceUrl" placeholder="https://example.com/style-source"></label>
        <label>Target <textarea id="subject" placeholder="one small object or image request"></textarea></label>
        <label>Run name <input id="runName" placeholder="optional"></label>
        <label>Include asset filter <input id="include" placeholder="/assets/"></label>
        <label>Max iterations <input id="maxIterations" type="number" min="1" max="20" value="5"></label>
        <button id="prepare" class="primary">Prepare Run</button>
      </section>

      <section id="lockedPanel" class="status"></section>
    </aside>

    <main>
      <section id="message"></section>
      <section class="panel">
        <div class="row" style="justify-content: space-between;">
          <div>
            <h2>Current Run</h2>
            <div id="runMeta" class="fine"></div>
          </div>
          <button id="refresh">Refresh</button>
        </div>
      </section>

      <section class="grid">
        <div class="panel">
          <h2>Reference Sheet</h2>
          <div id="contactSheet"></div>
        </div>
        <div class="panel">
          <h2>Comparison</h2>
          <div id="comparison"></div>
        </div>
      </section>

      <section class="panel">
        <h2>Candidates</h2>
        <div id="candidates" class="grid"></div>
      </section>

      <section class="panel">
        <h2>Run Files</h2>
        <div class="tabs">
          <button data-tab="prompt" class="active">Prompt</button>
          <button data-tab="taste_notes">Taste Notes</button>
          <button data-tab="agent_brief">Agent Brief</button>
          <button data-tab="next">Next Action</button>
        </div>
        <pre id="textPane"></pre>
      </section>
    </main>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let view = null;
    let selectedRuntime = 'codex';
    let activeTab = 'prompt';

    function fileUrl(path) {
      return `/api/file?path=${encodeURIComponent(path)}`;
    }

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: {'content-type': 'application/json'},
        ...options
      });
      const data = await res.json();
      if (!res.ok || data.status === 'error') {
        throw new Error(data.error || 'request failed');
      }
      return data;
    }

    function setMessage(text, kind = '') {
      $('message').innerHTML = text ? `<div class="status ${kind}">${text}</div>` : '';
    }

    function renderRuntimes() {
      const runtimes = view.runtimes || [];
      $('runtimes').innerHTML = runtimes.map((runtime) => {
        const cls = [
          runtime.name === selectedRuntime ? 'selected' : '',
          runtime.available ? '' : 'unavailable'
        ].join(' ');
        const label = `${runtime.name}${runtime.recommended ? ' · recommended' : ''}${runtime.available ? '' : ' · missing'}`;
        return `<button class="${cls}" data-runtime="${runtime.name}">${label}</button>`;
      }).join('');
      document.querySelectorAll('[data-runtime]').forEach((button) => {
        button.onclick = () => {
          selectedRuntime = button.dataset.runtime;
          renderRuntimes();
        };
      });
    }

    function renderLocked() {
      const locked = view.state?.locked_style;
      if (!locked) {
        $('lockedPanel').innerHTML = '<span class="pill warn">No locked style</span><div class="fine">Approve a cutout candidate to persist locked_style.</div>';
        return;
      }
      $('lockedPanel').innerHTML = `
        <span class="pill ok">Style locked</span>
        <div class="mono">${locked.run_name || ''}</div>
        <div class="fine">${locked.subject || ''}</div>
        ${locked.accepted_candidate ? `<a href="${fileUrl(locked.accepted_candidate)}" target="_blank">accepted candidate</a>` : ''}
      `;
    }

    function renderMeta() {
      const run = view.run || {};
      const runName = view.current_run || 'none';
      $('runMeta').innerHTML = `
        <span class="pill">${runName}</span>
        <span class="pill">${run.status || 'no status'}</span>
        <span class="pill">${run.reference_count || 0} refs</span>
        <span class="pill">max ${run.max_iterations || 5}</span>
        <div style="margin-top: 8px;">${run.source_url ? `<a href="${run.source_url}" target="_blank">${run.source_url}</a>` : 'No source URL yet.'}</div>
      `;
    }

    function renderImageSlot(id, path) {
      $(id).innerHTML = path
        ? `<a href="${fileUrl(path)}" target="_blank"><img class="hero-img" src="${fileUrl(path)}" alt="${path}"></a><div class="fine mono" style="margin-top: 8px;">${path}</div>`
        : '<div class="empty">No file yet.</div>';
    }

    function renderCandidates() {
      const candidates = [...(view.artifacts?.cutouts || []), ...(view.artifacts?.generated || [])];
      if (!candidates.length) {
        $('candidates').innerHTML = '<div class="empty">No generated candidates yet. Hand the agent brief to the selected runtime and refresh after it writes images.</div>';
        return;
      }
      $('candidates').innerHTML = candidates.map((item) => `
        <article class="artifact">
          <a href="${fileUrl(item.path)}" target="_blank"><img src="${fileUrl(item.path)}" alt="${item.name}"></a>
          <footer>
            <code>${item.name}</code>
            <button data-lock="${item.path}">Approve</button>
          </footer>
        </article>
      `).join('');
      document.querySelectorAll('[data-lock]').forEach((button) => {
        button.onclick = async () => {
          await api('/api/lock', {
            method: 'POST',
            body: JSON.stringify({candidate_path: button.dataset.lock})
          });
          await refresh();
          setMessage('Style locked from approved candidate.');
        };
      });
    }

    async function loadText(path) {
      if (!path) return 'No file yet.';
      const data = await api(`/api/text?path=${encodeURIComponent(path)}`);
      return data.text;
    }

    async function renderText() {
      if (activeTab === 'next') {
        const next = view.next_action?.recommended_next?.[0];
        $('textPane').textContent = next
          ? `${next.command}\n\n${next.why}`
          : JSON.stringify(view.next_action, null, 2);
        return;
      }
      $('textPane').textContent = await loadText(view.files?.[activeTab]);
    }

    async function refresh() {
      view = await api('/api/state');
      const foundCodex = (view.runtimes || []).find((runtime) => runtime.name === 'codex' && runtime.available);
      if (foundCodex) selectedRuntime = selectedRuntime || 'codex';
      renderRuntimes();
      renderLocked();
      renderMeta();
      renderImageSlot('contactSheet', view.files?.contact_sheet);
      renderImageSlot('comparison', view.files?.comparison);
      renderCandidates();
      await renderText();
    }

    async function prepare() {
      $('prepare').disabled = true;
      setMessage('Preparing run...');
      try {
        const include = $('include').value.trim() ? [$('include').value.trim()] : [];
        await api('/api/prepare', {
          method: 'POST',
          body: JSON.stringify({
            source_url: $('sourceUrl').value.trim(),
            subject: $('subject').value.trim(),
            run_name: $('runName').value.trim(),
            include,
            max_iterations: Number($('maxIterations').value || 5)
          })
        });
        await refresh();
        setMessage(`Run prepared. Open the agent brief in ${selectedRuntime}, then refresh as artifacts land.`);
      } catch (error) {
        setMessage(error.message, 'error');
      } finally {
        $('prepare').disabled = false;
      }
    }

    document.querySelectorAll('[data-tab]').forEach((button) => {
      button.onclick = async () => {
        document.querySelectorAll('[data-tab]').forEach((item) => item.classList.remove('active'));
        button.classList.add('active');
        activeTab = button.dataset.tab;
        await renderText();
      };
    });
    $('prepare').onclick = prepare;
    $('refresh').onclick = refresh;

    refresh().catch((error) => setMessage(error.message, 'error'));
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
