// Regenerates docs/screenshots/*.png from `kovan --demo` (synthetic data only).
//
//   python3 -m kovan --demo --no-browser      # serves on 127.0.0.1:8789
//   node scripts/take-screenshots.mjs [chrome-path]
//
// Needs Node 22+ (built-in fetch/WebSocket) and Chrome or Chromium.
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE = 'http://127.0.0.1:8789';
const OUT = new URL('../docs/screenshots/', import.meta.url).pathname;
const CHROME = process.argv[2] || [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
].find(existsSync);
if (!CHROME) throw new Error('Chrome not found; pass its path as the first argument');
mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = mkdtempSync(join(tmpdir(), 'kovan-shots-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--hide-scrollbars', '--remote-debugging-port=9334', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });

let target;
for (let i = 0; i < 60 && !target; i++) {
  try { target = (await (await fetch('http://127.0.0.1:9334/json/list')).json()).find((t) => t.type === 'page'); } catch {}
  if (!target) await sleep(200);
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let seq = 0;
const waiting = {};
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && waiting[m.id]) { waiting[m.id](m); delete waiting[m.id]; } };
const cmd = (method, params = {}) => new Promise((r) => { const id = ++seq; waiting[id] = r; ws.send(JSON.stringify({ id, method, params })); });
const js = async (expression) => (await cmd('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })).result?.result?.value;
const until = async (expression, ms = 15000) => { const end = Date.now() + ms; while (Date.now() < end) { if (await js(expression)) return true; await sleep(100); } return false; };
const shot = async (name) => {
  const r = await cmd('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(OUT, name), Buffer.from(r.result.data, 'base64'));
  console.log('saved', name);
};

const state = await (await fetch(`${BASE}/api/state`)).json();
const sid = (name) => state.sessions.find((s) => s.name === name).sid;

await cmd('Page.enable');
await cmd('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 2, mobile: false });
const theme = (value) => cmd('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value }] });
const open = async (hash, settle = 1500) => {
  await cmd('Page.navigate', { url: 'about:blank' });
  await cmd('Page.navigate', { url: `${BASE}/#${hash}` });
  await until('document.readyState === "complete" && !!window.S && !!S.d');
  await sleep(settle);
};
await theme('dark');

// 1. A kovan-driven session streaming its reply
await open(`chat/${sid('API refactor')}`, 800);
await until('(S.live[S.sel]||{}).text && S.live[S.sel].text.length > 260 && S.live[S.sel].text.length < 420', 30000);
await shot('chat.png');

// 2. Answering a question from the session
await open(`chat/${sid('Release prep')}`);
await js(`document.querySelector('.card-ask input[value="v2.4.0"]').checked = true;
          document.querySelector('.card-ask input[value="Blog"]').checked = true;
          document.querySelector('.card-ask input[value="Changelog page"]').checked = true;`);
await shot('question.png');

// 3. Tool call details: masked command output and a diff
await open(`chat/${sid('Security review')}`);
await js(`(async () => {
  for (const d of document.querySelectorAll('#c-list details.tools')) d.open = true;
  for (const li of document.querySelectorAll('#c-list li.call')) {
    if (/credentials|session\\.py/.test(li.textContent)) li.querySelector('[data-tool-open]').click();
  }
})()`);
await until('document.querySelectorAll("#c-list .detail pre, #c-list .detail .diff").length >= 3');
await sleep(400);
await js(`document.querySelector('#c-list li.call[data-tid] .detail').closest('details').scrollIntoView({block: 'start'})`);
await sleep(300);
await js(`document.querySelector('#jump').classList.remove('show')`);
await shot('tool-details.png');

// 4. Who talks to whom
await open('messages');
await js(`document.querySelector('details.msg').open = true`);
await sleep(300);
await shot('messages.png');

// 5. ⌘K search across sessions and notes
await open(`chat/${sid('Infra')}`);
await js(`openPalette(); const i = document.querySelector('#p-input'); i.value = 'staging'; i.dispatchEvent(new Event('input'));`);
await until('S && document.querySelectorAll("#p-list .p-hit").length > 3');
await sleep(300);
await shot('palette.png');

// 6. Overview
await open('overview');
await shot('overview.png');

// 7. The boardroom: a crew mid-build, one choosing an idea, the crew page and a swarm project's room
await open('board/crew:c7ab1e5f0', 2500);
await shot('boardroom.png');
await open('board/crew:c0dea1dea', 2500);
await shot('ideas.png');
await open('crews/c7ab1e5f0', 2000);
await shot('crew.png');
const swarmProject = (await (await fetch(`${BASE}/api/swarm`)).json()).projects[0].id;
await open(`board/swarm:${swarmProject}`, 3000);
await shot('swarm-room.png');

// 8. Turkish
await open('overview', 300);
await js(`localStorage.setItem('kovan.lang', 'tr')`);
await open('board/crew:c7ab1e5f0', 2500);
await shot('turkish.png');
await js(`localStorage.removeItem('kovan.lang')`);

// 9. Light theme
await theme('light');
await open(`chat/${sid('Infra')}`);
await js(`document.querySelector('[data-tasks]') && document.querySelector('[data-tasks]').click()`);
await sleep(700);
await shot('light.png');

console.log('errors in page:', await js('JSON.stringify(window.__errors || [])'));
ws.close();
chrome.kill();
