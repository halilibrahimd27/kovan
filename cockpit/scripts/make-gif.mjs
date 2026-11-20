// Records docs/boardroom.gif from `kovan --demo` (synthetic data only): the demo crew's meeting replayed
// message by message, so the table's arcs and the conversation build up the way they do live.
//
//   python3 -m kovan --demo --no-browser      # serves on 127.0.0.1:8789
//   node scripts/make-gif.mjs [chrome-path]   # needs ffmpeg on PATH
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE = 'http://127.0.0.1:8789';
const OUT = new URL('../docs/boardroom.gif', import.meta.url).pathname;
const CHROME = process.argv[2] || [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
].find(existsSync);
if (!CHROME) throw new Error('Chrome not found; pass its path as the first argument');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const work = mkdtempSync(join(tmpdir(), 'kovan-gif-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--hide-scrollbars', '--remote-debugging-port=9337', `--user-data-dir=${join(work, 'profile')}`, 'about:blank'], { stdio: 'ignore' });
let target;
for (let i = 0; i < 60 && !target; i++) {
  try { target = (await (await fetch('http://127.0.0.1:9337/json/list')).json()).find((t) => t.type === 'page'); } catch {}
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

await cmd('Page.enable');
await cmd('Emulation.setDeviceMetricsOverride', { width: 1280, height: 780, deviceScaleFactor: 1, mobile: false });
await cmd('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
await cmd('Page.navigate', { url: `${BASE}/#overview` });
await until('document.readyState === "complete"');
await js(`localStorage.setItem('kovan.lang', 'en'); true`);
await cmd('Page.navigate', { url: 'about:blank' });
await cmd('Page.navigate', { url: `${BASE}/#board/crew:c7ab1e5f0` });
await until('typeof S !== "undefined" && !!S.d && !!S.crewDetail && document.querySelectorAll(".say").length > 3');
await sleep(800);

// Replay the last part of the meeting: the board grows by one message per step.
const total = await js(`(window.__full = S.crewDetail.d.board.slice()).length`);
const first = total - 14;
const frames = [];
let n = 0;
const frame = async (seconds) => {
  const r = await cmd('Page.captureScreenshot', { format: 'png', clip: { x: 292, y: 0, width: 988, height: 780, scale: 1 } });
  const file = join(work, `f${String(n++).padStart(4, '0')}.png`);
  writeFileSync(file, Buffer.from(r.result.data, 'base64'));
  frames.push({ file, seconds });
};
for (let k = first; k <= total; k++) {
  await js(`S.crewDetail.d.board = window.__full.slice(0, ${k}); renderBoard(); true`);
  await sleep(250);
  const hold = k === total ? 3.0 : 1.1;
  await frame(hold / 2);           // two frames per step catch the pulsing rings
  await sleep(450);
  await frame(hold / 2);
}
chrome.kill();

const list = frames.map((f) => `file '${f.file}'\nduration ${f.seconds}`).join('\n') + `\nfile '${frames.at(-1).file}'\n`;
writeFileSync(join(work, 'frames.txt'), list);
const ff = (args) => { const r = spawnSync('ffmpeg', ['-y', '-loglevel', 'error', ...args], { stdio: 'inherit' }); if (r.status) throw new Error('ffmpeg failed'); };
ff(['-f', 'concat', '-safe', '0', '-i', join(work, 'frames.txt'), '-vf', 'scale=860:-1:flags=lanczos,palettegen=max_colors=96:stats_mode=diff', join(work, 'palette.png')]);
ff(['-f', 'concat', '-safe', '0', '-i', join(work, 'frames.txt'), '-i', join(work, 'palette.png'),
  '-lavfi', 'scale=860:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle', '-loop', '0', OUT]);
rmSync(work, { recursive: true, force: true });
console.log('saved', OUT, frames.length, 'frames');
process.exit(0);
