#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import net from 'node:net';

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith('--')) continue;
    const key = item.slice(2);
    args[key] = argv[index + 1];
    index += 1;
  }
  return args;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = address && typeof address === 'object' ? address.port : null;
      server.close(() => (port ? resolve(port) : reject(new Error('no port allocated'))));
    });
  });
}

function sanitize(value) {
  return String(value || 'element')
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40) || 'element';
}

class CdpClient {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    this.ws = new WebSocket(url);
    this.ready = new Promise((resolve, reject) => {
      this.ws.addEventListener('open', resolve, { once: true });
      this.ws.addEventListener('error', reject, { once: true });
    });
    this.ws.addEventListener('message', (event) => {
      const payload = JSON.parse(event.data);
      if (payload.id && this.pending.has(payload.id)) {
        const { resolve, reject } = this.pending.get(payload.id);
        this.pending.delete(payload.id);
        if (payload.error) reject(new Error(payload.error.message || JSON.stringify(payload.error)));
        else resolve(payload.result || {});
        return;
      }
      if (payload.method && this.listeners.has(payload.method)) {
        for (const listener of this.listeners.get(payload.method)) listener(payload.params || {});
      }
    });
  }

  async send(method, params = {}) {
    await this.ready;
    const id = this.nextId++;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
  }

  once(method, timeoutMs) {
    return new Promise((resolve, reject) => {
      const listener = (params) => {
        clearTimeout(timeout);
        this.listeners.set(method, (this.listeners.get(method) || []).filter((item) => item !== listener));
        resolve(params);
      };
      const timeout = setTimeout(() => {
        this.listeners.set(method, (this.listeners.get(method) || []).filter((item) => item !== listener));
        reject(new Error(`timed out waiting for ${method}`));
      }, timeoutMs);
      this.listeners.set(method, [...(this.listeners.get(method) || []), listener]);
    });
  }

  close() {
    this.ws.close();
  }
}

async function waitForPageEndpoint(port) {
  const listUrl = `http://127.0.0.1:${port}/json/list`;
  const newUrl = `http://127.0.0.1:${port}/json/new?about:blank`;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const res = await fetch(listUrl);
      if (res.ok) {
        const targets = await res.json();
        const page = targets.find((target) => target.type === 'page' && target.webSocketDebuggerUrl);
        if (page) return page.webSocketDebuggerUrl;
      }
      if (attempt === 8) {
        await fetch(newUrl, { method: 'PUT' }).catch(() => null);
      }
    } catch {
      // Chrome is still booting.
    }
    await sleep(250);
  }
  throw new Error('Chrome page DevTools endpoint did not become ready');
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const chrome = args.chrome;
  const sourceUrl = args.url;
  const outDir = args['out-dir'];
  const maxElements = Number(args['max-elements'] || 24);
  if (!chrome || !sourceUrl || !outDir) {
    throw new Error('usage: browser_capture_elements.mjs --chrome <path> --url <url> --out-dir <dir> [--max-elements N]');
  }

  const port = await freePort();
  const userDataDir = mkdtempSync(join(tmpdir(), 'stylekit-chrome-'));
  mkdirSync(join(outDir, 'browser', 'elements'), { recursive: true });

  const proc = spawn(chrome, [
    '--headless=new',
    '--disable-gpu',
    '--disable-dev-shm-usage',
    '--hide-scrollbars',
    '--no-first-run',
    '--no-default-browser-check',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userDataDir}`,
    'about:blank',
  ], { stdio: ['ignore', 'ignore', 'pipe'] });

  let stderr = '';
  proc.stderr.on('data', (chunk) => {
    stderr += chunk.toString();
  });

  let client;
  try {
    const pageWs = await waitForPageEndpoint(port);
    client = new CdpClient(pageWs);
    await client.send('Page.enable');
    await client.send('Runtime.enable');
    await client.send('DOM.enable');
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: 1440,
      height: 1200,
      deviceScaleFactor: 1,
      mobile: false,
    });
    const loaded = client.once('Page.loadEventFired', 25000).catch(() => null);
    await client.send('Page.navigate', { url: sourceUrl });
    await loaded;
    await sleep(2500);

    const html = await client.send('Runtime.evaluate', {
      expression: 'document.documentElement.outerHTML',
      returnByValue: true,
    });
    writeFileSync(join(outDir, 'browser', 'page.html'), html.result.value || '');

    const screenshot = await client.send('Page.captureScreenshot', {
      format: 'png',
      captureBeyondViewport: true,
      fromSurface: true,
    });
    writeFileSync(join(outDir, 'browser', 'page-screenshot.png'), Buffer.from(screenshot.data, 'base64'));

    const metrics = await client.send('Page.getLayoutMetrics');
    const content = metrics.contentSize || { width: 1440, height: 1200 };
    const expression = `(() => {
      const selectors = [
        'img[src]',
        'svg',
        'canvas',
        '[style*="grid-template-columns"]',
        '[data-header="true"]',
        '[data-msg="true"]',
        '.card-brutal',
        '.btn-brutal',
        '.btn-brutal-sm',
        'a[class*="btn"]',
        'button'
      ];
      const nodes = [...document.querySelectorAll(selectors.join(','))];
      const seen = new Set();
      const items = [];
      for (const el of nodes) {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;
        if (rect.width < 18 || rect.height < 18) continue;
        if (rect.width > 900 || rect.height > 760) continue;
        if (rect.bottom < 0 || rect.right < 0 || rect.top > innerHeight * 1.8 || rect.left > innerWidth) continue;
        const key = [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)].join(':');
        if (seen.has(key)) continue;
        seen.add(key);
        const label = el.getAttribute('alt') || el.getAttribute('aria-label') || el.textContent.trim().replace(/\\s+/g, ' ').slice(0, 48) || el.tagName.toLowerCase();
        const tag = el.tagName.toLowerCase();
        const priority =
          tag === 'img' ? 1 :
          tag === 'svg' ? 2 :
          el.matches('[style*="grid-template-columns"]') ? 3 :
          el.matches('[data-header="true"], [data-msg="true"], .card-brutal') ? 4 :
          5;
        items.push({
          x: rect.left + scrollX,
          y: rect.top + scrollY,
          width: rect.width,
          height: rect.height,
          tag,
          label,
          priority,
        });
      }
      return items
        .sort((a, b) => a.priority - b.priority || a.y - b.y || a.x - b.x)
        .slice(0, ${JSON.stringify(maxElements)});
    })()`;
    const evaluated = await client.send('Runtime.evaluate', { expression, returnByValue: true });
    const elements = evaluated.result.value || [];
    const references = [{
      reference: 'browser/page-screenshot.png',
      label: 'full page screenshot',
      source_file: `${sourceUrl}#browser`,
    }];

    let index = 1;
    for (const item of elements) {
      const padding = 8;
      const x = Math.max(0, Math.floor(item.x - padding));
      const y = Math.max(0, Math.floor(item.y - padding));
      const width = Math.min(Math.ceil(item.width + padding * 2), Math.max(1, Math.floor(content.width - x)));
      const height = Math.min(Math.ceil(item.height + padding * 2), Math.max(1, Math.floor(content.height - y)));
      if (width <= 1 || height <= 1) continue;
      const stem = `element-${String(index).padStart(2, '0')}-${sanitize(item.tag)}-${sanitize(item.label)}`;
      const reference = `browser/elements/${stem}.png`;
      const crop = await client.send('Page.captureScreenshot', {
        format: 'png',
        fromSurface: true,
        clip: { x, y, width, height, scale: 1 },
      });
      writeFileSync(join(outDir, reference), Buffer.from(crop.data, 'base64'));
      references.push({
        reference,
        label: item.label,
        tag: item.tag,
        source_file: `${sourceUrl}#browser-element`,
        box: { x, y, width, height },
      });
      index += 1;
    }

    writeFileSync(join(outDir, 'browser', 'elements.json'), JSON.stringify(references, null, 2) + '\n');
    process.stdout.write(JSON.stringify({ references }, null, 2) + '\n');
  } finally {
    if (client) client.close();
    proc.kill('SIGTERM');
    try {
      rmSync(userDataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    } catch {
      // Chrome can keep cache files alive briefly after SIGTERM; this must not
      // turn a successful capture into a failed fallback.
    }
    if (stderr.trim()) process.stderr.write(stderr);
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
