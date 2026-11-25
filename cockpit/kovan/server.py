"""kovan server: watches Claude Code sessions, drives the ones you hand to it, serves the web UI.

Run ``kovan --help`` for options. Everything stays on this machine:

* WATCH   — reads ``~/.claude/sessions/<pid>.json`` (live status) and
            ``~/.claude/projects/<dir>/*.jsonl`` (transcripts) and pushes changes to the browser
            over Server-Sent Events. Sessions running in VS Code or a terminal are watched read-only.
* DRIVE   — "Continue in kovan" / "New session" start ``claude`` in stream-json mode (the same
            mode the official IDE integrations and the Agent SDK use). The processes live in a
            separate holder (``kovan.holder``) so restarting this server never kills a session.
* SECURE  — binds 127.0.0.1 only, checks the Host header, requires Origin + a per-start token
            for every state-changing request, masks likely secrets in tool output, and never
            writes message content to its log.
"""
import argparse
import base64
import binascii
import collections
import datetime as dt
import glob
import hashlib
import hmac
import html
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__

HOME = os.path.expanduser('~')
CONFIG_DIR = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.join(HOME, '.config'), 'kovan')
STATE_DIR = os.path.join(os.environ.get('XDG_STATE_HOME') or os.path.join(HOME, '.local', 'state'), 'kovan')
SETTINGS_PATH = os.path.join(CONFIG_DIR, 'settings.json')   # managed by the UI
CONFIG_PATH = os.path.join(CONFIG_DIR, 'config.json')       # optional, hand-edited defaults
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
HOLDER_PROTOCOL = 1
DEFAULT_PORT = 8787
DEMO_PORT = 8789

SUBAGENT_RE = re.compile(r'^a[0-9a-f]{16}$')
PEER_MSG_RE = re.compile(r'<cross-session-message[^>]*?from-name="([^"]*)"[^>]*>\n?(.*?)\n?</cross-session-message>', re.S)
SUMMARY_RE = re.compile(r'<summary>(.*?)</summary>', re.S)
TASK_ID_RE = re.compile(r'<task-id>(.*?)</task-id>', re.S)
TASK_STATUS_RE = re.compile(r'<status>(.*?)</status>', re.S)
COMMAND_RE = re.compile(r'<command-name>(.*?)</command-name>', re.S)
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
TASK_ID_OK = re.compile(r'^[A-Za-z0-9_-]{4,64}$')
MODEL_OK = re.compile(r'^[a-z0-9][a-z0-9.\-\[\]]{1,60}$')
TOOL_USE_ID_RE = re.compile(rb'"tool_use_id":"([^"]+)"')
BG_TASK_RE = re.compile(rb'"backgroundTaskId":"([^"]+)"')
SKIP_PREFIXES = (b'{"type":"api-request', b'{"type":"file-history', b'{"type":"atis', b'{"type":"session-owner',
                 b'{"type":"queue-operation', b'{"type":"last-prompt', b'{"type":"tools-used')
NOISY_TOOLS = {'ToolSearch', 'TodoWrite', 'TaskList', 'TaskGet', 'TaskOutput'}
EDIT_TOOLS = ('Edit', 'Write', 'NotebookEdit', 'MultiEdit')
EVENT_LIMIT = 5000
TEXT_LIMIT = 12000
IMAGE_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']
PERMISSION_MODES = ['default', 'acceptEdits', 'plan', 'bypassPermissions']
DEFAULT_QUICK_PROMPTS = [
    "Give me a short status update: what's done, what's left, what's blocked?",
    'Continue.',
    'Review the changes you made so far for security issues and list your findings.',
    'Summarize what you changed and why, as a commit message.',
]

SECRET_PATTERNS = [
    (re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)', re.S),
     '[private key hidden]'),
    (re.compile(r'(?i)(\b[\w.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|'
                r'credential|parola|sifre)[\w.-]*\b["\']?\s*[:=]\s*)(["\']?)([^\s"\'`,;]{3,})'), r'\1\2••••'),
    (re.compile(r'(?i)(authorization\s*:\s*(?:bearer|basic|token)\s+)[^\s"\']+'), r'\1••••'),
    (re.compile(r'(://[^/\s:@"\']+:)[^@\s/"\']{3,}(@)'), r'\1••••\2'),
    (re.compile(r'\b(?:gl(?:pat|dt|rt|cbt|ptt|oas)-[A-Za-z0-9_\-]{10,}|sk-ant-[A-Za-z0-9_\-]{10,}|ghp_[A-Za-z0-9]{20,}|'
                r'github_pat_[A-Za-z0-9_]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|'
                r'eyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{10,})'), '••••'),
]
FOLD_TABLE = str.maketrans({'ı': 'i', 'ş': 's', 'ğ': 'g', 'ü': 'u', 'ö': 'o', 'ç': 'c', 'â': 'a', 'î': 'i',
                            'û': 'u', 'é': 'e', 'è': 'e', 'ê': 'e', 'á': 'a', 'à': 'a', 'ñ': 'n', 'ä': 'a'})


# ============================================================================ small helpers
def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


def project_folder(claude_home, directory):
    """Claude Code stores transcripts under projects/<path with non-alphanumerics replaced by '-'>."""
    return os.path.join(claude_home, 'projects', re.sub(r'[^A-Za-z0-9]', '-', os.path.abspath(directory)))


def ts_ms(s):
    if not s:
        return None
    try:
        return int(dt.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def clip(s, n):
    s = (s or '').strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def head_tail(s, head=24000, tail=8000):
    s = s or ''
    if len(s) <= head + tail:
        return s
    return s[:head] + '\n\n… (%d characters omitted) …\n\n' % (len(s) - head - tail) + s[-tail:]


def tilde(p):
    return p.replace(HOME, '~', 1) if p and p.startswith(HOME) else (p or '')


def norm_ws(s):
    return re.sub(r'\s+', ' ', s or '').strip()[:200]


def fold(s):
    """Case- and accent-insensitive form of ``s`` with the SAME length, so match offsets map back."""
    t = s.replace('İ', 'i').replace('I', 'i').lower().translate(FOLD_TABLE)
    if len(t) == len(s):
        return t
    return ''.join((unicodedata.normalize('NFD', c.lower()) or c)[0] for c in s).translate(FOLD_TABLE)


def mask_secrets(s):
    if not s or not isinstance(s, str):
        return s, 0
    n = 0
    for pattern, repl in SECRET_PATTERNS:
        s, c = pattern.subn(repl, s)
        n += c
    return s, n


def trim_input(d):
    if not isinstance(d, dict):
        return d
    return {k: (v[:30000] + '…' if isinstance(v, str) and len(v) > 30000 else v) for k, v in d.items()}


def message_text(content):
    """Human-visible text of a user message (drops IDE/system reminder blocks)."""
    if isinstance(content, str):
        m = COMMAND_RE.search(content)
        return m.group(1) if m else content
    if not isinstance(content, list):
        return ''
    parts = []
    for b in content:
        if not isinstance(b, dict):
            continue
        kind = b.get('type')
        if kind == 'text':
            text = b.get('text') or ''
            m = COMMAND_RE.search(text)
            if m:
                parts.append(m.group(1))
                continue
            if text.lstrip().startswith(('<ide_', '<system-reminder', '<local-command', '<command-')):
                continue
            parts.append(text)
        elif kind == 'image':
            parts.append('[image]')
        elif kind == 'document':
            parts.append('[document]')
    return '\n'.join(parts)


def tool_label(name, inp):
    g = inp if isinstance(inp, dict) else {}
    if name == 'Bash':
        return (g.get('description') or 'ran a command') + (' (background)' if g.get('run_in_background') else '')
    if name == 'Read':
        return 'read ' + tilde(g.get('file_path', ''))
    if name in ('Edit', 'NotebookEdit', 'MultiEdit'):
        return 'edited ' + tilde(g.get('file_path') or g.get('notebook_path') or '')
    if name == 'Write':
        return 'wrote ' + tilde(g.get('file_path', ''))
    if name == 'Grep':
        return 'searched for ' + clip(str(g.get('pattern', '')), 60)
    if name == 'Glob':
        return 'listed ' + clip(str(g.get('pattern', '')), 60)
    if name in ('Agent', 'Task'):
        return 'started subagent: ' + str(g.get('description', ''))
    if name == 'Skill':
        return 'skill: ' + str(g.get('skill', ''))
    if name == 'WebSearch':
        return 'web search: ' + clip(str(g.get('query', '')), 80)
    if name == 'WebFetch':
        return 'fetched ' + clip(str(g.get('url', '')), 80)
    if name == 'SendMessage':
        return '→ ' + str(g.get('to', '')) + ': ' + clip(str(g.get('summary') or g.get('message', '')), 80)
    if name == 'AskUserQuestion':
        return 'asked you a question'
    if name == 'ExitPlanMode':
        return 'asked to approve a plan'
    if name.startswith('mcp__'):
        return name.split('__', 2)[-1]
    return name


class KovanError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ============================================================================ settings (UI-managed)
class Settings:
    def __init__(self, path, read_only=False):
        self.path = path
        self.read_only = read_only
        self.lock = threading.Lock()
        self.data = {'archived': [], 'aliases': {}, 'quick_prompts': list(DEFAULT_QUICK_PROMPTS), 'dirs': [], 'roles': []}
        self.revision = 0
        try:
            with open(path, encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for key in ('archived', 'aliases', 'quick_prompts', 'dirs', 'roles'):
                    if key in loaded:
                        self.data[key] = loaded[key]
        except (OSError, ValueError):
            pass

    def _save(self):
        self.revision += 1
        if self.read_only:
            return
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def is_archived(self, sid):
        return sid in self.data['archived']

    def add_dir(self, directory):
        """A directory you started a session in from the UI; watched again after a restart."""
        with self.lock:
            if directory not in self.data['dirs']:
                self.data['dirs'] = self.data['dirs'] + [directory]
                self._save()

    def set_archived(self, sid, archived):
        with self.lock:
            items = [x for x in self.data['archived'] if x != sid]
            if archived:
                items.append(sid)
            self.data['archived'] = items
            self._save()

    def alias(self, sid):
        return self.data['aliases'].get(sid)

    def set_alias(self, sid, name):
        with self.lock:
            if (name or None) == self.data['aliases'].get(sid):
                return
            if name:
                self.data['aliases'][sid] = name
            else:
                self.data['aliases'].pop(sid, None)
            self._save()

    def quick_prompts(self):
        return list(self.data.get('quick_prompts') or [])

    def roles(self):
        """Crew roles you defined and asked kovan to remember for later crews."""
        return [r for r in self.data.get('roles') or [] if isinstance(r, dict)]

    def set_roles(self, items):
        from .crew import clean_custom_role
        if not isinstance(items, list) or len(items) > 30:
            raise KovanError('at most 30 saved roles')
        clean, seen = [], set()
        for item in items:
            role = clean_custom_role(item)
            if role['title'].lower() not in seen:
                seen.add(role['title'].lower())
                clean.append(role)
        with self.lock:
            self.data['roles'] = clean
            self._save()

    def set_quick_prompts(self, items):
        if not isinstance(items, list) or len(items) > 30:
            raise KovanError('at most 30 quick prompts')
        clean = [str(x).strip()[:4000] for x in items if str(x).strip()]
        with self.lock:
            self.data['quick_prompts'] = clean
            self._save()


# ============================================================================ transcript reader
class Transcript:
    """One session's .jsonl transcript. Reads only the bytes appended since the last call."""

    def __init__(self, sid, path, directory):
        self.sid, self.path, self.directory = sid, path, directory
        self.reset()

    def reset(self):
        self.offset = 0
        self.partial = b''
        self.titles = []
        self.events = collections.deque(maxlen=EVENT_LIMIT)
        self.sent, self.received = [], []
        self.seen_peer_msgs = set()
        self.prompt_count = self.tool_count = 0
        self.files = collections.OrderedDict()
        self.first_ts = self.last_ts = None
        self.context_tokens = self.model = self.effort = None
        self.first_prompt = None
        self.counter = 0
        self.inputs = {}         # tool_use_id -> (tool name, trimmed input)
        self.results = {}        # tool_use_id -> (file offset, length); content is read on demand only
        self.tool_events = {}    # tool_use_id -> event
        self.tasks = collections.OrderedDict()
        self.folded = {}

    def add(self, kind, ts, **fields):
        self.counter += 1
        ev = {'id': '%s-%d' % (self.sid[:8], self.counter), 'kind': kind, 'ts': ts, 'sid': self.sid}
        ev.update(fields)
        self.events.append(ev)
        return ev

    def update(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return
        if size < self.offset:
            self.reset()
        if size == self.offset:
            return
        with open(self.path, 'rb') as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        buf = self.partial + data
        pos = self.offset - len(self.partial)
        self.offset += len(data)
        lines = buf.split(b'\n')
        self.partial = lines.pop()
        for line in lines:
            start, pos = pos, pos + len(line) + 1
            if not line or line.startswith(SKIP_PREFIXES):
                continue
            if line.startswith(b'{"parentUuid"') and b'"type":"user"' in line[:400] and b'"tool_use_id"' in line[:700]:
                # A tool result: remember where it is, but never keep its content in memory.
                m = TOOL_USE_ID_RE.search(line, 0, 1500)
                if m:
                    tid = m.group(1).decode('utf-8', 'replace')
                    self.results[tid] = (start, len(line))
                    ev = self.tool_events.get(tid)
                    if ev is not None and b'"is_error":true' in line[:4000]:
                        ev['error'] = True
                    bg = BG_TASK_RE.search(line)
                    if bg:
                        self.task_started(bg.group(1).decode('utf-8', 'replace'), tid)
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            try:
                self.process(d)
            except Exception:  # noqa: BLE001 - a malformed record must not stop the reader
                pass

    def task_started(self, task_id, tid):
        ev = self.tool_events.get(tid)
        t = self.tasks.setdefault(task_id, {'id': task_id, 'status': 'running', 'ended_at': None, 'summary': ''})
        t.update({'tid': tid, 'text': (ev or {}).get('text', ''), 'ts': (ev or {}).get('ts')})

    def process(self, d):
        kind = d.get('type')
        if kind == 'custom-title':
            title = d.get('customTitle')
            if title and (not self.titles or self.titles[-1] != title):
                self.titles.append(title)
            return
        if d.get('isSidechain'):
            return
        ts = ts_ms(d.get('timestamp'))
        if ts:
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = max(self.last_ts or 0, ts)
        if kind == 'user':
            origin = d.get('origin') or {}
            okind = origin.get('kind')
            content = (d.get('message') or {}).get('content')
            if okind == 'human' or (okind is None and d.get('promptSource') == 'sdk' and not d.get('isMeta')
                                    and not d.get('isCompactSummary')
                                    and not (isinstance(content, str) and content.lstrip().startswith('<local-command'))):
                self.prompt(message_text(content), ts, d.get('uuid'))
            elif okind == 'task-notification':
                text = content if isinstance(content, str) else message_text(content)
                m = SUMMARY_RE.search(text or '')
                summary = clip(m.group(1) if m else 'background task finished', 400)
                self.add('notice', ts, text=summary)
                tm, sm = TASK_ID_RE.search(text or ''), TASK_STATUS_RE.search(text or '')
                if tm:
                    task_id = tm.group(1).strip()
                    t = self.tasks.setdefault(task_id, {'id': task_id, 'text': summary, 'ts': None, 'tid': None})
                    t.update({'status': sm.group(1).strip() if sm else 'completed', 'ended_at': ts, 'summary': summary})
            elif okind == 'peer':
                self.peer_message(origin, content, ts)
        elif kind == 'attachment':
            a = d.get('attachment') or {}
            if a.get('type') == 'queued_command':
                origin = a.get('origin') or {}
                if origin.get('kind') == 'human':
                    self.prompt(message_text(a.get('prompt')), ts, None)
                elif origin.get('kind') == 'peer':
                    self.peer_message(origin, a.get('prompt'), ts)
        elif kind == 'assistant':
            msg = d.get('message') or {}
            if msg.get('model') and not msg['model'].startswith('<'):
                self.model = msg['model']
            if d.get('effort'):
                self.effort = d['effort']
            usage = msg.get('usage') or {}
            if usage.get('input_tokens') is not None:
                self.context_tokens = (usage.get('input_tokens') or 0) + (usage.get('cache_read_input_tokens') or 0) + \
                                      (usage.get('cache_creation_input_tokens') or 0)
            for b in msg.get('content') or []:
                btype = b.get('type')
                if btype == 'text':
                    text = (b.get('text') or '').strip()
                    if text:
                        self.add('reply', ts, text=clip(text, TEXT_LIMIT))
                elif btype == 'tool_use':
                    name = b.get('name', '')
                    inp = b.get('input') or {}
                    tid = b.get('id')
                    if name in NOISY_TOOLS:
                        continue
                    self.tool_count += 1
                    if name == 'SendMessage':
                        to = str(inp.get('to', ''))
                        ev = self.add('sent', ts, to=to, summary=str(inp.get('summary') or ''),
                                      text=clip(str(inp.get('message', '')), TEXT_LIMIT), subagent=bool(SUBAGENT_RE.match(to)))
                        self.sent.append(ev)
                        continue
                    path = None
                    if name in EDIT_TOOLS:
                        path = tilde(inp.get('file_path') or inp.get('notebook_path') or '') or None
                        if path:
                            self.files[path] = ts
                            self.files.move_to_end(path)
                    ev = self.add('tool', ts, tool=name, text=tool_label(name, inp), tid=tid, file=path)
                    if tid:
                        self.inputs[tid] = (name, trim_input(inp))
                        self.tool_events[tid] = ev

    def prompt(self, text, ts, msg_uuid):
        text = (text or '').strip()
        if not text:
            return
        self.prompt_count += 1
        if self.first_prompt is None:
            self.first_prompt = text
        self.add('prompt', ts, text=clip(text, TEXT_LIMIT), uuid=msg_uuid)

    def peer_message(self, origin, content, ts):
        mid = origin.get('msg_id') or origin.get('delivery_id')
        if mid:
            if mid in self.seen_peer_msgs:
                return
            self.seen_peer_msgs.add(mid)
        name, body = origin.get('name'), origin.get('body')
        if not body:
            text = content if isinstance(content, str) else message_text(content)
            m = PEER_MSG_RE.search(text or '')
            if m:
                name, body = name or m.group(1), m.group(2)
        ev = self.add('received', ts, sender=name or '?', pid=origin.get('verifiedPeerPid'),
                      text=clip(body or '', TEXT_LIMIT), subagent=bool(origin.get('senderTaskId')))
        self.received.append(ev)

    def folded_text(self, ev):
        f = self.folded.get(ev['id'])
        if f is None:
            f = fold(ev.get('text') or '')
            self.folded[ev['id']] = f
        return f


# ============================================================================ store
class Store:
    def __init__(self, claude_home, dirs, notes, task_roots=None):
        self.claude_home = claude_home
        self.sessions_dir = os.path.join(claude_home, 'sessions')
        self.dirs = [os.path.abspath(d) for d in dirs]
        self.folders = {d: project_folder(claude_home, d) for d in self.dirs}
        self.notes_paths = notes
        self.task_roots = task_roots or sorted({tempfile.gettempdir(), '/tmp', '/private/tmp'})
        self.transcripts = {}
        self.lock = threading.RLock()
        self.extra_revision = 0

    def add_dir(self, directory):
        directory = os.path.abspath(directory)
        with self.lock:
            if directory not in self.folders:
                self.dirs.append(directory)
                self.folders[directory] = project_folder(self.claude_home, directory)

    # ---- sources
    def live_sessions(self):
        """sessionId -> live session record (from ~/.claude/sessions). '_pids' lists every live process."""
        groups = collections.defaultdict(list)
        for p in glob.glob(os.path.join(self.sessions_dir, '*.json')):
            try:
                with open(p, encoding='utf-8') as f:
                    d = json.load(f)
                pid = int(d.get('pid'))
            except Exception:  # noqa: BLE001
                continue
            try:
                os.kill(pid, 0)
            except PermissionError:
                pass
            except Exception:  # noqa: BLE001
                continue
            if UUID_RE.match(str(d.get('sessionId') or '')):
                groups[d['sessionId']].append(d)
        ours = MANAGER.pids() if MANAGER else set()
        out = {}
        for sid, items in groups.items():
            items.sort(key=lambda d: (d.get('pid') not in ours, -(d.get('updatedAt') or 0)))
            d = dict(items[0])
            d['_pids'] = [x.get('pid') for x in items]
            out[sid] = d
        return out

    def scan(self, live):
        for directory, folder in self.folders.items():
            for p in glob.glob(os.path.join(folder, '*.jsonl')):
                sid = os.path.basename(p)[:-6]
                if UUID_RE.match(sid) and sid not in self.transcripts:
                    self.transcripts[sid] = Transcript(sid, p, directory)
        for sid, c in live.items():
            cwd = os.path.abspath(c.get('cwd') or '/')
            if cwd in self.folders and sid not in self.transcripts:
                self.transcripts[sid] = Transcript(sid, os.path.join(self.folders[cwd], sid + '.jsonl'), cwd)
        if MANAGER:
            for sid, s in MANAGER.owned_items():
                if sid not in self.transcripts and s.directory in self.folders:
                    self.transcripts[sid] = Transcript(sid, os.path.join(self.folders[s.directory], sid + '.jsonl'), s.directory)
        for t in self.transcripts.values():
            t.update()

    def subagents(self, t, live):
        out = []
        folder = self.folders.get(t.directory)
        for p in glob.glob(os.path.join(folder, t.sid, 'subagents', '*.meta.json')):
            try:
                with open(p, encoding='utf-8') as f:
                    m = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            try:
                last = int(os.path.getmtime(p[:-len('.meta.json')] + '.jsonl') * 1000)
            except OSError:
                last = None
            out.append({'id': os.path.basename(p)[len('agent-'):-len('.meta.json')], 'description': m.get('description', ''),
                        'type': m.get('agentType', ''), 'running': bool(live and not m.get('finished')), 'last': last})
        out.sort(key=lambda x: x['last'] or 0, reverse=True)
        return out

    def process_started_at(self, sid, c):
        s = MANAGER.owned(sid) if MANAGER else None
        if s and s.started_at:
            return int(s.started_at * 1000)
        return (c or {}).get('startedAt')

    def task_list(self, t, live, started_at):
        """Background tasks. Tasks started before the current process started cannot still be running."""
        out = []
        for task in reversed(list(t.tasks.values())):
            status = task.get('status') or 'running'
            if status == 'running' and (not live or (started_at and task.get('ts') and task['ts'] < started_at - 2000)):
                status = 'unknown'
            out.append(dict(task, status=status))
        return out[:25]

    def colors(self):
        order = sorted(self.transcripts.values(), key=lambda t: (t.first_ts or 1e15, t.sid))
        return {t.sid: i for i, t in enumerate(order)}

    def name(self, t, c):
        alias = SETTINGS.alias(t.sid) if SETTINGS else None
        if alias:
            return alias
        if t.titles:
            return t.titles[-1]
        if c and c.get('name'):
            return c['name']
        s = MANAGER.owned(t.sid) if MANAGER else None
        if s and s.name:
            return s.name
        if t.first_prompt:
            return clip(t.first_prompt.replace('\n', ' '), 36)
        return t.sid[:8]

    def summary(self, t, c, color, now):
        latest = {}
        for ev in reversed(t.events):
            if ev['kind'] not in latest:
                latest[ev['kind']] = ev
                if len(latest) == 6:
                    break
        s = MANAGER.owned(t.sid) if MANAGER else None
        raw = c.get('status') if c else None
        if not c and not s:
            status = 'closed'
        elif raw == 'busy':
            status = 'working'
        elif raw == 'idle' or s:
            status = 'your_turn' if now - (t.last_ts or 0) < 30 * 60 * 1000 else 'idle'
        else:
            status = 'other'
        turn_started = max([latest[k]['ts'] or 0 for k in ('prompt', 'received', 'notice') if k in latest] or [0]) or None
        current = None
        if status == 'working':
            last_ev = t.events[-1] if t.events else None
            if last_ev and last_ev['kind'] == 'tool':
                current = {'ts': last_ev['ts'], 'text': last_ev['text']}
            else:
                current = {'ts': last_ev['ts'] if last_ev else None, 'text': 'thinking…'}
        if s:
            if s.permissions:
                status = 'awaiting'
                current = {'ts': None, 'text': 'waiting for your answer'}
            elif s.status == 'working':
                status = 'working'
                current = {'ts': None, 'text': (s.tool or {}).get('text') or ('writing…' if s.streaming else 'thinking…')}
                turn_started = s.turn_started_ms() or turn_started
            elif s.status == 'starting':
                status, current = 'starting', None
            elif status == 'working':
                status, current = 'your_turn', None
        entry = (c or {}).get('entrypoint') or ''
        place = 'kovan' if s else ('vscode' if c and 'vscode' in entry else ('elsewhere' if c else 'closed'))
        name = self.name(t, c)
        former = []
        for n in ([x.get('name') for x in (c or {}).get('formerNames', [])] + t.titles[:-1] + ([c.get('name')] if c else [])):
            if n and n != name and n not in former:
                former.append(n)
        live = bool(c or s)
        subs = self.subagents(t, bool(c))

        def cut(ev, n):
            return {'ts': ev['ts'], 'text': clip(ev['text'], n)} if ev else None

        return {
            'sid': t.sid, 'name': name, 'former_names': former, 'color': color, 'directory': tilde(t.directory),
            'topic': clip((t.first_prompt or '').replace('\n', ' '), 180),
            'status': status, 'raw_status': raw, 'live': live, 'place': place,
            'archived': bool(SETTINGS and SETTINGS.is_archived(t.sid)),
            'conflict': bool(s and c and len(c.get('_pids', [])) > 1),
            'started_at': (c or {}).get('startedAt') or t.first_ts, 'last_activity': t.last_ts, 'turn_started_at': turn_started,
            'prompt_count': t.prompt_count, 'tool_count': t.tool_count, 'file_count': len(t.files),
            'context_tokens': t.context_tokens, 'model': (s.model if s and s.model else None) or t.model, 'effort': t.effort,
            'last_prompt': cut(latest.get('prompt'), 500), 'last_reply': cut(latest.get('reply'), 900), 'now': current,
            'sent': sum(1 for e in t.sent if not e['subagent']), 'received': sum(1 for e in t.received if not e['subagent']),
            'subagents_total': len(subs), 'subagents_running': sum(1 for a in subs if a['running']),
            'tasks_running': sum(1 for x in self.task_list(t, live, self.process_started_at(t.sid, c)) if x['status'] == 'running'),
            'managed': s.snapshot() if s else None,
            'crew': CREWS.role_of(t.sid) if CREWS else None,
        }

    def ordered_summaries(self, live, now):
        colors = self.colors()
        items = [self.summary(t, live.get(sid), colors[sid], now) for sid, t in self.transcripts.items()]
        rank = {'awaiting': 0, 'working': 0, 'starting': 1, 'other': 1, 'your_turn': 1, 'idle': 2, 'closed': 3}
        items.sort(key=lambda o: (rank.get(o['status'], 4), -(o['last_activity'] or 0)))
        return items

    # ---- inter-session messages
    def alias_map(self, live):
        names = {}
        for sid, t in self.transcripts.items():
            for title in t.titles:
                names[title.casefold()] = sid
        for sid, c in live.items():
            if sid in self.transcripts:
                for n in [c.get('name')] + [x.get('name') for x in c.get('formerNames', [])]:
                    if n:
                        names[n.casefold()] = sid
        return names

    def pid_map(self, live):
        out = {}
        for sid, c in live.items():
            if sid in self.transcripts:
                for p in c.get('_pids', [c.get('pid')]):
                    out[p] = sid
        return out

    @staticmethod
    def resolve_target(to, names, pids):
        m = re.match(r'^uds:.*?/(\d+)\.sock$', to or '')
        if m:
            return pids.get(int(m.group(1)))
        return names.get((to or '').casefold())

    @staticmethod
    def target_label(to):
        m = re.match(r'^uds:.*?/(\d+)\.sock$', to or '')
        return ('session (pid %s)' % m.group(1)) if m else to

    def with_current_names(self, events, live):
        names, pids = self.alias_map(live), self.pid_map(live)
        out = []
        for ev in events:
            if ev['kind'] == 'sent' and not ev['subagent']:
                sid = self.resolve_target(ev['to'], names, pids)
                ev = dict(ev, to=self.name(self.transcripts[sid], live.get(sid)), to_sid=sid) if sid else \
                    dict(ev, to=self.target_label(ev['to']))
            elif ev['kind'] == 'received' and not ev['subagent']:
                sid = pids.get(ev.get('pid')) or names.get((ev['sender'] or '').casefold())
                if sid:
                    ev = dict(ev, sender=self.name(self.transcripts[sid], live.get(sid)), sender_sid=sid)
            out.append(ev)
        return out

    def messages(self, live):
        names, pids = self.alias_map(live), self.pid_map(live)
        sub_names, out, by_body = {}, [], {}
        for sid, t in self.transcripts.items():
            if any(e['subagent'] for e in t.sent):
                for a in self.subagents(t, False):
                    sub_names[a['id']] = a['description']
            for e in t.sent:
                to = e['to']
                m = {'id': e['id'], 'ts': e['ts'], 'from': sid, 'from_name': None,
                     'to': None if e['subagent'] else self.resolve_target(to, names, pids),
                     'to_name': ('subagent: ' + sub_names.get(to, to)) if e['subagent'] else self.target_label(to),
                     'summary': e['summary'], 'text': clip(e['text'], 6000), 'subagent': e['subagent'], 'delivered': False}
                out.append(m)
                by_body.setdefault(norm_ws(e['text']), []).append(m)
        for sid, t in self.transcripts.items():
            for e in t.received:
                candidates = [m for m in by_body.get(norm_ws(e['text']), []) if m['from'] != sid and not m['delivered']]
                if candidates:
                    candidates[0]['delivered'] = True
                    candidates[0]['to'] = sid
                    continue
                out.append({'id': e['id'], 'ts': e['ts'], 'from': pids.get(e.get('pid')) or names.get((e['sender'] or '').casefold()),
                            'from_name': ('subagent: ' + e['sender']) if e['subagent'] else e['sender'], 'to': sid,
                            'to_name': None, 'summary': '', 'text': clip(e['text'], 6000), 'subagent': e['subagent'],
                            'delivered': True})
        out.sort(key=lambda m: m['ts'] or 0, reverse=True)
        return out[:400]

    def feed(self, live):
        main, tools = [], []
        for t in self.transcripts.values():
            for ev in t.events:
                (tools if ev['kind'] == 'tool' else main).append(ev)
        main.sort(key=lambda e: e['ts'] or 0, reverse=True)
        tools.sort(key=lambda e: e['ts'] or 0, reverse=True)
        out = main[:500] + tools[:300]
        out.sort(key=lambda e: e['ts'] or 0, reverse=True)
        return [dict(e, text=clip(e.get('text', ''), 2500)) for e in self.with_current_names(out, live)]

    def revision(self, live):
        parts = ['%s:%d' % (sid, t.offset) for sid, t in sorted(self.transcripts.items())]
        parts += ['%s:%s:%s:%s' % (sid, c.get('status'), c.get('name'), c.get('_pids')) for sid, c in sorted(live.items())]
        if MANAGER:
            parts.append(MANAGER.signature())
        if SETTINGS:
            parts.append('settings%d' % SETTINGS.revision)
        parts.append('extra%d' % self.extra_revision)
        if CREWS:
            parts.append('crews%d' % CREWS.revision)
        return hashlib.sha1('|'.join(parts).encode()).hexdigest()[:16]

    def mark_task(self, sid, task_id, status):
        with self.lock:
            t = self.transcripts.get(sid)
            if t and task_id in t.tasks:
                t.tasks[task_id]['status'] = status
                self.extra_revision += 1

    # ---- API payloads
    def state(self, client_revision, with_feed):
        with self.lock:
            live = self.live_sessions()
            self.scan(live)
            rev = self.revision(live) + ('f' if with_feed else '')
            if client_revision == rev:
                return {'unchanged': True}
            now = int(time.time() * 1000)
            out = {'revision': rev, 'now': now, 'version': __version__, 'dirs': [tilde(d) for d in self.dirs],
                   'sessions': self.ordered_summaries(live, now), 'messages': self.messages(live),
                   'limit': MANAGER.limit if MANAGER else None, 'quick_prompts': SETTINGS.quick_prompts() if SETTINGS else [],
                   'holder_connected': bool(HOLDER and HOLDER.connected), 'demo': CFG.demo,
                   'notes_available': bool(self.notes_paths)}
            if with_feed:
                out['feed'] = self.feed(live)
            return out

    def session(self, sid, client_revision):
        with self.lock:
            live = self.live_sessions()
            self.scan(live)
            t = self.transcripts.get(sid)
            if not t:
                raise KovanError('session not found', 404)
            c = live.get(sid) or {}
            s = MANAGER.owned(sid) if MANAGER else None
            rev = '%d:%s:%s:%s:%d:%d' % (t.offset, c.get('status'), c.get('name'), s.signature() if s else '-',
                                         SETTINGS.revision if SETTINGS else 0, self.extra_revision)
            if client_revision == rev:
                return {'unchanged': True}
            events = list(t.events)[-900:]
            live_now = bool(c or s)
            return {'revision': rev,
                    'summary': self.summary(t, live.get(sid), self.colors()[sid], int(time.time() * 1000)),
                    'events': self.with_current_names(events, live), 'truncated': max(0, t.counter - len(events)),
                    'files': [{'path': p, 'ts': ts} for p, ts in reversed(list(t.files.items()))][:80],
                    'subagents': self.subagents(t, bool(live.get(sid))),
                    'tasks': self.task_list(t, live_now, self.process_started_at(sid, c)),
                    'managed': s.snapshot(full=True) if s else None}

    def tool_detail(self, sid, tid):
        with self.lock:
            t = self.transcripts.get(sid)
            if not t or (tid not in t.inputs and tid not in t.results):
                raise KovanError('no record of this tool call', 404)
            name, inp = t.inputs.get(tid, (None, None))
            loc = t.results.get(tid)
            path = t.path
        masked = 0
        out = {'tid': tid, 'tool': name, 'input': None, 'result': None}
        if isinstance(inp, dict):
            clean = {}
            for k, v in inp.items():
                if isinstance(v, str):
                    v, n = mask_secrets(v)
                    masked += n
                clean[k] = v
            if name == 'Write' and isinstance(clean.get('content'), str):
                clean['content'] = head_tail(clean['content'], 20000, 4000)
            out['input'] = clean
        if loc:
            try:
                with open(path, 'rb') as f:
                    f.seek(loc[0])
                    d = json.loads(f.read(loc[1]))
            except (OSError, ValueError):
                d = None
            if d:
                block = ((d.get('message') or {}).get('content') or [{}])[0]
                inner = block.get('content') if isinstance(block, dict) else None
                if isinstance(inner, list):
                    text = '\n'.join(b.get('text', '') if b.get('type') == 'text' else '[%s]' % b.get('type')
                                     for b in inner if isinstance(b, dict))
                else:
                    text = str(inner or '')
                tur = d.get('toolUseResult') if isinstance(d.get('toolUseResult'), dict) else {}
                if name == 'Bash' and ('stdout' in tur or 'stderr' in tur):
                    text = (tur.get('stdout') or '') + (('\n' + tur['stderr']) if tur.get('stderr') else '')
                    if tur.get('interrupted'):
                        text += '\n[interrupted]'
                text, n = mask_secrets(head_tail(text))
                masked += n
                result = {'text': text, 'error': bool(block.get('is_error')) if isinstance(block, dict) else False}
                patch = tur.get('structuredPatch')
                if isinstance(patch, list) and patch:
                    count, hunks = 0, []
                    for h in patch:
                        lines = []
                        for line in h.get('lines') or []:
                            if count >= 1500:
                                break
                            line, n = mask_secrets(str(line))
                            masked += n
                            lines.append(line)
                            count += 1
                        hunks.append({'oldStart': h.get('oldStart'), 'oldLines': h.get('oldLines'),
                                      'newStart': h.get('newStart'), 'newLines': h.get('newLines'), 'lines': lines})
                    result['patch'] = hunks
                    result['patch_truncated'] = count >= 1500
                elif name == 'Write' and tur.get('type') == 'create' and isinstance(tur.get('content'), str):
                    content, n = mask_secrets(head_tail(tur['content'], 30000, 2000))
                    masked += n
                    result['new_file'] = content
                if tur.get('filePath'):
                    result['file'] = tilde(tur['filePath'])
                if tur.get('backgroundTaskId'):
                    result['task'] = tur['backgroundTaskId']
                out['result'] = result
        out['masked'] = masked
        return out

    def task_output(self, sid, task_id):
        if not TASK_ID_OK.match(task_id or ''):
            raise KovanError('invalid task id')
        with self.lock:
            t = self.transcripts.get(sid)
            task = dict(t.tasks.get(task_id) or {}) if t else {}
        candidates = []
        for root in self.task_roots:
            candidates += glob.glob(os.path.join(root, 'claude-%d' % os.getuid(), '*', sid, 'tasks', task_id + '.output'))
        if not candidates:
            return {'id': task_id, 'status': task.get('status'), 'text': '', 'missing': True}
        p = candidates[0]
        try:
            size = os.path.getsize(p)
            with open(p, 'rb') as f:
                if size > 40000:
                    f.seek(size - 40000)
                text = f.read().decode('utf-8', 'replace')
        except OSError:
            size, text = 0, ''
        text, n = mask_secrets(text)
        return {'id': task_id, 'status': task.get('status'), 'text': text, 'truncated': size > 40000, 'size': size,
                'masked': n, 'label': task.get('text')}

    def search(self, q):
        q = (q or '').strip()
        if len(q) < 2:
            return {'q': q, 'results': []}
        fq = fold(q)
        out = []
        with self.lock:
            for sid, t in self.transcripts.items():
                for ev in reversed(t.events):
                    if ev['kind'] not in ('prompt', 'reply', 'sent', 'received', 'tool', 'notice'):
                        continue
                    i = t.folded_text(ev).find(fq)
                    if i < 0:
                        continue
                    text = ev.get('text') or ''
                    a, b = max(0, i - 70), min(len(text), i + len(q) + 110)
                    out.append({'sid': sid, 'id': ev['id'], 'kind': ev['kind'], 'ts': ev['ts'],
                                'before': ('…' if a > 0 else '') + text[a:i].replace('\n', ' '), 'match': text[i:i + len(q)],
                                'after': text[i + len(q):b].replace('\n', ' ') + ('…' if b < len(text) else '')})
        out.sort(key=lambda x: x['ts'] or 0, reverse=True)
        out = out[:80]
        for note in self.read_notes(limit_entries=None):
            sections = note['entries'] if note['kind'] == 'journal' else [{'heading': note['title'], 'body': note['content']}]
            for sec in sections:
                whole = sec['heading'] + '\n' + sec['body']
                i = fold(whole).find(fq)
                if i < 0:
                    continue
                a, b = max(0, i - 70), min(len(whole), i + len(q) + 110)
                out.append({'sid': None, 'kind': 'note', 'note': note['title'], 'heading': sec['heading'], 'ts': None,
                            'before': ('…' if a > 0 else '') + whole[a:i].replace('\n', ' '), 'match': whole[i:i + len(q)],
                            'after': whole[i + len(q):b].replace('\n', ' ') + '…'})
                if len(out) >= 100:
                    return {'q': q, 'results': out}
        return {'q': q, 'results': out}

    def dir_suggestions(self, limit=400):
        """Folders to offer for a new session: the watched ones, then the folders directly inside them."""
        with self.lock:
            dirs = list(self.dirs)
        out = list(dirs)
        for d in dirs:
            try:
                names = sorted(os.listdir(d), key=str.lower)
            except OSError:
                continue
            out += [os.path.join(d, n) for n in names if not n.startswith('.') and os.path.isdir(os.path.join(d, n))]
        seen = []
        for x in out:
            if x not in seen:
                seen.append(x)
        return [tilde(x) for x in seen[:limit]]

    def read_notes(self, limit_entries=60):
        notes = []
        for path in self.notes_paths:
            try:
                with open(path, encoding='utf-8') as f:
                    text = f.read()
                modified = int(os.path.getmtime(path) * 1000)
            except OSError:
                continue
            if text.startswith('---'):
                text = text.split('---', 2)[-1].strip()
            title = os.path.basename(path)
            entries = journal_entries(text)
            if entries is not None:
                notes.append({'title': title, 'path': tilde(path), 'modified': modified, 'kind': 'journal',
                              'entries': entries[:limit_entries] if limit_entries else entries})
            else:
                notes.append({'title': title, 'path': tilde(path), 'modified': modified, 'kind': 'doc', 'content': text})
        return notes


DATED_HEADING = re.compile(r'^(?:\d|(?:mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
                           r'pazartesi|sal[ıi]|çarşamba|carsamba|perşembe|persembe|cuma|cumartesi|pazar)\w*\b)', re.I)


def journal_entries(text):
    """Entries of a journal-style markdown file, newest first; None when the file is a plain document.

    A journal has dated `##`/`###` headings ("## 12 Kasım 2026", "### 20 Nov 15:02 — …", "## Mon — …"). Each
    `###` is an entry, labelled with the bare `##` above it (the day); a `##` with its own text is an entry.
    Headings inside fenced code blocks are ignored."""
    lines, in_code, heads = text.split('\n'), False, []
    for i, line in enumerate(lines):
        if line.lstrip().startswith('```'):
            in_code = not in_code
        elif not in_code:
            m = re.match(r'^(#{2,3}) (.+)$', line)
            if m:
                heads.append((i, len(m.group(1)), m.group(2).strip()))
    if sum(1 for _, _, h in heads if DATED_HEADING.match(h)) < 2:
        return None
    entries, group = [], None
    for n, (i, level, heading) in enumerate(heads):
        stop = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = '\n'.join(lines[i + 1:stop]).strip()
        if level == 2:            # a bare "## 12 Kasım 2026" groups what follows; a "##" with text is an entry
            group = None if body else heading
            if body:
                entries.append({'heading': heading, 'group': None, 'body': clip(body, 8000)})
        else:
            entries.append({'heading': heading, 'group': group, 'body': clip(body, 8000)})
    return entries[::-1]


# ============================================================================ live push (SSE)
class Bus:
    def __init__(self):
        self.queues = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=5000)
        with self.lock:
            self.queues.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.queues.discard(q)

    def publish(self, event):
        data = json.dumps(event, ensure_ascii=False)
        with self.lock:
            for q in list(self.queues):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass


# ============================================================================ driving sessions
def find_claude():
    if CFG.claude_bin:
        return CFG.claude_bin if os.access(CFG.claude_bin, os.X_OK) else None
    on_path = shutil.which('claude')
    if on_path:
        return on_path
    local = os.path.join(HOME, '.local', 'bin', 'claude')
    if os.access(local, os.X_OK):
        return local

    def version(p):
        m = re.search(r'claude-code-(\d+)\.(\d+)\.(\d+)', p)
        return tuple(int(x) for x in m.groups()) if m else (0,)
    bundled = [p for p in glob.glob(os.path.join(HOME, '.vscode*', 'extensions', 'anthropic.claude-code-*', 'resources',
                                                 'native-binary', 'claude')) if os.access(p, os.X_OK)]
    return max(bundled, key=version) if bundled else None


def child_env():
    """Environment for kovan's own child processes: makes the package importable without clobbering PYTHONPATH."""
    env = dict(os.environ)
    if 'KOVAN_ORIG_PYTHONPATH' not in env:
        env['KOVAN_ORIG_PYTHONPATH'] = env.get('PYTHONPATH', '')
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env['PYTHONPATH'] = os.pathsep.join(x for x in (pkg_parent, env['KOVAN_ORIG_PYTHONPATH']) if x)
    return env


def session_env():
    """Child sessions never inherit this shell's Claude/VS Code variables (messaging tokens, session ids…)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('CLAUDE', 'VSCODE_', 'ELECTRON_', 'KOVAN_')) and k not in ('MCP_CONNECTION_NONBLOCKING', 'PYTHONPATH')}
    if os.environ.get('KOVAN_ORIG_PYTHONPATH'):
        env['PYTHONPATH'] = os.environ['KOVAN_ORIG_PYTHONPATH']
    elif 'KOVAN_ORIG_PYTHONPATH' not in os.environ and os.environ.get('PYTHONPATH'):
        env['PYTHONPATH'] = os.environ['PYTHONPATH']
    env.update({'CLAUDE_CODE_ENTRYPOINT': 'sdk-cli', 'MCP_CONNECTION_NONBLOCKING': 'true',
                'CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING': 'true', 'CLAUDE_CODE_QUESTION_PREVIEW_FORMAT': 'markdown'})
    return env


class HolderClient:
    """Talks to ``kovan.holder`` over its Unix socket."""

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.sock = None
        self.write_lock = threading.Lock()
        self.connect_lock = threading.Lock()
        self.waiting = {}
        self.connected = False
        self.ready = threading.Event()

    def _try(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.socket_path)
            return s
        except OSError:
            s.close()
            return None

    def spawn_holder(self):
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        logf = open(os.path.join(STATE_DIR, 'holder.log'), 'ab')
        subprocess.Popen([sys.executable, '-m', 'kovan.holder', '--socket', self.socket_path], stdout=logf,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, cwd=HOME,
                         env=child_env())
        log('holder started')

    def connect(self, spawn=False):
        with self.connect_lock:
            if self.connected:
                return True
            s = self._try()
            if not s and spawn:
                self.spawn_holder()
                for _ in range(60):
                    time.sleep(0.1)
                    s = self._try()
                    if s:
                        break
            if not s:
                return False
            self.sock, self.connected = s, True
            self.ready.clear()
            threading.Thread(target=self.reader, args=(s,), daemon=True, name='holder-reader').start()
            self.send({'op': 'hello'})
            self.send({'op': 'subscribe'})
        self.ready.wait(15)
        return True

    def send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
        with self.write_lock:
            if not self.connected or not self.sock:
                raise KovanError('not connected to the session holder', 503)
            try:
                self.sock.sendall(data)
            except OSError:
                self.connected = False
                raise KovanError('could not write to the session holder', 503)

    def start_session(self, sid, args, cwd, env, info):
        if not self.connect(spawn=True):
            raise KovanError('could not start the session holder', 500)
        ev, box = threading.Event(), []
        self.waiting[sid] = (ev, box)
        self.send({'op': 'start', 'sid': sid, 'args': args, 'cwd': cwd, 'env': env, 'info': info})
        if not ev.wait(15):
            self.waiting.pop(sid, None)
            raise KovanError('the session holder did not answer in time', 504)
        r = box[0]
        if r.get('event') == 'error':
            raise KovanError(r.get('message') or 'could not start the session', 500)
        return r.get('pid')

    def reader(self, s):
        try:
            for raw in s.makefile('rb'):
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                try:
                    self.dispatch(msg)
                except Exception as e:  # noqa: BLE001
                    log('could not handle holder event: %r' % e)
        except OSError:
            pass
        finally:
            if self.sock is s:
                self.connected = False
                self.ready.set()
                log('lost connection to the session holder')
                for sid, session in MANAGER.owned_items():
                    session.exited(None, ['lost connection to the session holder'])
                threading.Thread(target=self.reconnect_loop, daemon=True).start()

    def reconnect_loop(self):
        for _ in range(720):
            time.sleep(5)
            if self.connected:
                return
            if self.connect(spawn=False):
                log('reconnected to the session holder')
                return

    def dispatch(self, msg):
        kind = msg.get('event')
        sid = msg.get('sid')
        if kind == 'output':
            s = MANAGER.sessions.get(sid)
            if s and s.status != 'closed':
                s.handle_line(msg.get('line') or '', msg.get('t'))
        elif kind == 'session':
            MANAGER.adopt(sid, msg.get('pid'), msg.get('info') or {}, msg.get('started_at'))
        elif kind == 'ready':
            for s in list(MANAGER.sessions.values()):
                if s.replaying:
                    s.replay_done()
            self.ready.set()
            BUS.publish({'type': 'changed', 'sessions': []})
        elif kind in ('started', 'error'):
            waiter = self.waiting.pop(sid, None)
            if waiter:
                waiter[1].append(msg)
                waiter[0].set()
            elif kind == 'error':
                log('holder error %s: %s' % ((sid or '')[:8], msg.get('message')))
        elif kind == 'exited':
            s = MANAGER.sessions.get(sid)
            if s:
                s.exited(msg.get('code'), msg.get('stderr') or [])
        elif kind == 'hello' and msg.get('protocol') != HOLDER_PROTOCOL:
            log('warning: holder protocol %s, expected %s' % (msg.get('protocol'), HOLDER_PROTOCOL))


class ManagedSession:
    """A session whose claude process kovan drives (through the holder)."""

    def __init__(self, sid, name, resume, directory, pid=None, permission_mode=None, model=None, effort=None,
                 extra_args=None):
        self.sid, self.name, self.resume, self.directory, self.pid = sid, name, resume, directory, pid
        self.launch_mode = permission_mode      # None = CFG.permission_mode
        self.launch_model, self.launch_effort, self.extra_args = model, effort, list(extra_args or [])
        self.last_result, self.last_result_at = '', None   # final text of the last finished turn (crews read it)
        self.last_usage = None                  # tokens of the last finished turn (crews add them up)
        self.status = 'starting'
        self.permissions = collections.OrderedDict()
        self.streaming = ''
        self.thinking = False
        self.tool = None
        self.turn_started = None
        self.commands, self.models = [], []
        self.model = self.permission_mode = self.status_text = self.context = None
        self.exit_code = None
        self.replaying = False
        self.waiting = {}
        self.started_at = None

    # ---- helpers
    def turn_started_ms(self):
        return int(self.turn_started * 1000) if self.turn_started else None

    def publish(self, kind, **fields):
        if self.replaying:
            return
        ev = {'type': kind, 'sid': self.sid}
        ev.update(fields)
        BUS.publish(ev)

    def set_status(self, status, t=None):
        if status == self.status:
            return
        self.turn_started = (t or time.time()) if status == 'working' else None
        self.status = status
        self.publish('status', status=status, turn_started_at=self.turn_started_ms())

    def snapshot(self, full=False):
        out = {'status': self.status, 'permissions': list(self.permissions.values()), 'tool': self.tool,
               'thinking': self.thinking, 'turn_started_at': self.turn_started_ms(), 'model': self.model,
               'permission_mode': self.permission_mode, 'context': self.context, 'status_text': self.status_text,
               'pid': self.pid}
        if full:
            out.update({'streaming': self.streaming, 'commands': self.commands, 'models': self.models})
        return out

    def signature(self):
        return '%s:%s:%d:%s:%s:%s:%s' % (self.sid[:8], self.status, len(self.permissions), (self.tool or {}).get('text'),
                                         self.permission_mode, self.model, (self.context or {}).get('total'))

    # ---- process (via the holder)
    def start(self):
        binary = find_claude()
        if not binary:
            raise KovanError('could not find the claude executable (set --claude-bin)', 500)
        args = [binary, '--output-format', 'stream-json', '--verbose', '--input-format', 'stream-json',
                '--permission-prompt-tool=stdio', '--setting-sources=user,project,local',
                '--permission-mode=' + (self.launch_mode or CFG.permission_mode), '--include-partial-messages',
                '--replay-user-messages',
                # Like the IDE integrations: makes "Never ask" *selectable* later from the UI. It does not turn it on;
                # the session starts in CFG.permission_mode ("default" = ask) unless you choose otherwise.
                '--allow-dangerously-skip-permissions']
        if self.launch_model:
            args.append('--model=' + self.launch_model)
        if self.launch_effort:
            args.append('--effort=' + self.launch_effort)
        args += self.extra_args
        if self.resume:
            args.append('--resume=' + self.sid)
        else:
            args += ['--session-id', self.sid]
            if self.name:
                args += ['--name', self.name]
        self.started_at = time.time()
        self.pid = HOLDER.start_session(self.sid, args, self.directory, session_env(),
                                        {'name': self.name, 'resume': self.resume, 'dir': self.directory})
        self.write({'type': 'control_request', 'request_id': 'init-' + self.sid[:8],
                    'request': {'subtype': 'initialize', 'hooks': None}})
        log('action: session started %s (%s) pid=%s' % (self.sid[:8], 'resume' if self.resume else 'new', self.pid))

    def write(self, obj):
        HOLDER.send({'op': 'write', 'sid': self.sid, 'data': json.dumps(obj, ensure_ascii=False)})

    def request(self, subtype, params=None, timeout=20):
        rid = 'kovan-' + secrets.token_hex(6)
        ev, box = threading.Event(), []
        self.waiting[rid] = (ev, box)
        req = {'subtype': subtype}
        req.update(params or {})
        self.write({'type': 'control_request', 'request_id': rid, 'request': req})
        if not ev.wait(timeout):
            self.waiting.pop(rid, None)
            raise KovanError('the session did not answer in time (%s)' % subtype, 504)
        r = box[0]
        if r.get('subtype') != 'success':
            raise KovanError('the session refused: %s' % (r.get('error') or subtype), 409)
        return r.get('response') or {}

    def refresh_context(self):
        try:
            r = self.request('get_context_usage', timeout=25)
        except Exception:  # noqa: BLE001
            return
        self.context = {'total': r.get('totalTokens'), 'max': r.get('maxTokens'), 'percent': r.get('percentage'),
                        'threshold': r.get('autoCompactThreshold')}
        self.publish('context', context=self.context)

    def replay_done(self):
        self.replaying = False
        self.publish('status', status=self.status, turn_started_at=self.turn_started_ms())
        threading.Thread(target=self.refresh_context, daemon=True).start()

    def exited(self, code, stderr):
        if self.status == 'closed':
            return
        self.exit_code = code
        self.permissions.clear()
        self.streaming, self.tool, self.thinking, self.turn_started = '', None, False, None
        self.status = 'closed'
        for ev, box in list(self.waiting.values()):
            box.append({'subtype': 'error', 'error': 'session closed'})
            ev.set()
        self.waiting.clear()
        error = '' if code in (0, -15) else clip(' | '.join(stderr or []), 600)
        log('session closed %s code=%s' % (self.sid[:8], code))
        self.replaying = False
        self.publish('process', state='closed', code=code, error=error)

    def handle_line(self, line, t):
        try:
            d = json.loads(line)
        except ValueError:
            return
        try:
            self.handle(d, t)
        except Exception as e:  # noqa: BLE001
            log('could not handle event %s: %r' % (self.sid[:8], e))

    def handle(self, d, t=None):
        kind = d.get('type')
        if kind == 'control_response':
            r = d.get('response') or {}
            rid = str(r.get('request_id', ''))
            waiter = self.waiting.pop(rid, None)
            if waiter:
                waiter[1].append(r)
                waiter[0].set()
            if rid.startswith('init-'):
                if r.get('subtype') == 'success':
                    resp = r.get('response') or {}
                    self.commands = [{'name': c.get('name'), 'description': clip(c.get('description') or '', 140),
                                      'hint': c.get('argumentHint') or ''} for c in resp.get('commands') or [] if c.get('name')]
                    self.models = [{'value': m.get('value'), 'name': m.get('displayName') or m.get('value')}
                                   for m in resp.get('models') or [] if m.get('value')]
                    self.permission_mode = resp.get('current_permission_mode') or self.permission_mode
                if self.status == 'starting':
                    self.set_status('ready')
                    if not self.replaying:
                        threading.Thread(target=self.refresh_context, daemon=True).start()
            return
        if kind == 'control_request':
            req = d.get('request') or {}
            rid = d.get('request_id')
            if req.get('subtype') == 'can_use_tool':
                item = {'id': rid, 'tool': req.get('tool_name'), 'input': req.get('input') or {},
                        'title': req.get('title') or req.get('display_name'), 'description': req.get('description'),
                        'ts': int((t or time.time()) * 1000)}
                self.permissions[rid] = item
                self.publish('permission', request=item)
            elif not self.replaying:
                self.write({'type': 'control_response', 'response': {
                    'subtype': 'error', 'request_id': rid, 'error': 'kovan does not support %s' % req.get('subtype')}})
            return
        if kind == 'control_cancel_request':
            if self.permissions.pop(d.get('request_id'), None):
                self.publish('permission_done', id=d.get('request_id'))
            return
        if kind == 'rate_limit_event':
            MANAGER.update_limit(d.get('rate_limit_info') or {})
            return
        if kind == 'system':
            sub = d.get('subtype')
            if sub == 'init':
                self.model = d.get('model') or self.model
                self.permission_mode = d.get('permissionMode') or self.permission_mode
            elif sub == 'status':
                self.status_text = d.get('status')
                self.publish('status_text', text=self.status_text)
            return
        if d.get('parent_tool_use_id'):
            return      # subagent stream
        if kind == 'stream_event':
            ev = d.get('event') or {}
            et = ev.get('type')
            if et == 'message_start':
                self.set_status('working', t)
            elif et == 'content_block_start':
                block = ev.get('content_block') or {}
                bt = block.get('type')
                if bt == 'text':
                    self.streaming, self.thinking = '', False
                    self.publish('text_start')
                elif bt in ('thinking', 'redacted_thinking'):
                    self.thinking = True
                    self.publish('thinking')
                elif bt in ('tool_use', 'server_tool_use'):
                    self.tool = {'text': 'preparing %s…' % block.get('name', 'a tool'), 'name': block.get('name')}
                    self.thinking = False
                    self.publish('tool', tool=self.tool)
            elif et == 'content_block_delta':
                delta = ev.get('delta') or {}
                if delta.get('type') == 'text_delta' and delta.get('text'):
                    self.streaming += delta['text']
                    self.publish('chunk', text=delta['text'])
            return
        if kind == 'assistant':
            had_text = False
            for b in (d.get('message') or {}).get('content') or []:
                if b.get('type') == 'tool_use':
                    self.tool = {'text': tool_label(b.get('name', ''), b.get('input')), 'name': b.get('name')}
                    self.publish('tool', tool=self.tool)
                elif b.get('type') == 'text':
                    had_text = True
            if had_text:
                self.streaming = ''
                self.publish('text_end')
            return
        if kind == 'user':
            content = (d.get('message') or {}).get('content')
            if isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
                self.tool, self.thinking = None, True
                self.publish('tool', tool=None)
            elif not (isinstance(content, str) and content.lstrip().startswith('<local-command')):
                self.set_status('working', t)
                self.publish('received')
            return
        if kind == 'result':
            self.last_result, self.last_result_at = str(d.get('result') or ''), (t or time.time())
            u = d.get('usage') or {}
            last = (u.get('iterations') or [{}])[-1] or {}
            self.last_usage = {'in': u.get('input_tokens') or 0, 'out': u.get('output_tokens') or 0,
                               'cache_read': u.get('cache_read_input_tokens') or 0,
                               'cache_write': u.get('cache_creation_input_tokens') or 0, 'ms': d.get('duration_ms') or 0,
                               'context': sum(last.get(k) or 0 for k in ('input_tokens', 'cache_read_input_tokens',
                                                                       'cache_creation_input_tokens')) or None}
            self.streaming, self.tool, self.thinking = '', None, False
            if self.permissions:
                self.permissions.clear()
                self.publish('permission_done', id=None)
            self.set_status('ready')
            summary = re.sub(r'\s+', ' ', re.sub(r'[*`#>|_]', '', str(d.get('result') or '')))
            self.publish('turn_done', error=bool(d.get('is_error')), summary=clip(summary, 240), duration_ms=d.get('duration_ms'))
            if not self.replaying:
                threading.Thread(target=self.refresh_context, daemon=True).start()

    # ---- actions
    def send(self, text, images):
        if images:
            content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': i['type'], 'data': i['data']}} for i in images]
            if text:
                content.append({'type': 'text', 'text': text})
        else:
            content = text
        # Same shape the IDE integrations send; origin=human marks it as a human prompt in the transcript.
        self.write({'type': 'user', 'message': {'role': 'user', 'content': content}, 'parent_tool_use_id': None,
                    'session_id': self.sid, 'uuid': str(uuid.uuid4()), 'origin': {'kind': 'human'}})
        self.set_status('working')
        log('action: message sent %s (%d chars, %d images)' % (self.sid[:8], len(text), len(images)))

    def interrupt(self):
        self.write({'type': 'control_request', 'request_id': 'stop-' + secrets.token_hex(4), 'request': {'subtype': 'interrupt'}})
        log('action: interrupt %s' % self.sid[:8])

    def answer_permission(self, rid, decision, answers, message):
        item = self.permissions.get(rid)
        if not item:
            raise KovanError('this request is no longer pending', 409)
        if decision == 'allow':
            new_input = dict(item['input'])
            if answers is not None:
                new_input['answers'] = {str(k): str(v) for k, v in answers.items()}
            response = {'behavior': 'allow', 'updatedInput': new_input}
        else:
            response = {'behavior': 'deny', 'message': (message or 'The user declined.')[:4000]}
        self.write({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': rid, 'response': response}})
        self.permissions.pop(rid, None)
        self.publish('permission_done', id=rid)
        log('action: permission %s %s %s' % (self.sid[:8], item['tool'], decision))

    def set_model(self, model):
        r = self.request('set_model', {'model': model})
        self.model = r.get('model') or model
        log('action: model %s -> %s' % (self.sid[:8], self.model))
        self.publish('info')
        return self.model

    def set_permission_mode(self, mode):
        r = self.request('set_permission_mode', {'mode': mode})
        self.permission_mode = r.get('mode') or mode
        log('action: permission mode %s -> %s' % (self.sid[:8], self.permission_mode))
        self.publish('info')
        return self.permission_mode

    def rename(self, name):
        self.request('rename_session', {'title': name, 'source': 'host', 'session_id': self.sid})
        self.name = name
        log('action: renamed %s' % self.sid[:8])

    def rewind(self, msg_uuid, dry_run):
        r = self.request('rewind_files', {'user_message_id': msg_uuid, 'dry_run': bool(dry_run)}, timeout=90)
        if not dry_run:
            log('action: files rewound %s (%d files)' % (self.sid[:8], len(r.get('filesChanged') or [])))
        return r

    def stop_task(self, task_id):
        self.request('stop_task', {'task_id': task_id})
        log('action: background task stopped %s %s' % (self.sid[:8], task_id))

    def release(self):
        log('action: release %s' % self.sid[:8])
        HOLDER.send({'op': 'close', 'sid': self.sid})


def session_directory(directory):
    """Where a new session runs: any existing directory you pick. One that is not watched yet joins the watched
    list (and stays there), so the session shows up and survives restarts."""
    directory = os.path.abspath(os.path.expanduser(str(directory).strip()))
    if directory == '/' or not os.path.isdir(directory):
        raise KovanError('no such directory: %s' % tilde(directory))
    if directory not in (MANAGER.dirs if MANAGER else []):
        if STORE:
            STORE.add_dir(directory)
        if MANAGER:
            MANAGER.add_dir(directory)
        if SETTINGS:
            SETTINGS.add_dir(directory)
        log('action: watching %s' % tilde(directory))
    return directory


class Manager:
    def __init__(self, dirs):
        self.dirs = [os.path.abspath(d) for d in dirs]
        self.sessions = {}
        self.lock = threading.Lock()
        self.limit = None

    def owned(self, sid):
        s = self.sessions.get(sid)
        return s if s and s.status != 'closed' else None

    def owned_items(self):
        return [(sid, s) for sid, s in list(self.sessions.items()) if s.status != 'closed']

    def pids(self):
        return {s.pid for _, s in self.owned_items() if s.pid}

    def signature(self):
        return ','.join(s.signature() for _, s in self.owned_items()) + ':%s' % ((self.limit or {}).get('ts'))

    def update_limit(self, info):
        windows = info.get('unifiedWindows') or {}

        def window(key):
            w = windows.get(key) or {}
            u = w.get('utilization')
            return {'percent': round(u * 100) if isinstance(u, (int, float)) else None,
                    'resets_at': w.get('resetsAt'), 'status': w.get('status')} if w else None
        new = {'five_hour': window('five_hour'), 'seven_day': window('seven_day'), 'status': info.get('status'),
               'ts': int(time.time())}
        if not self.limit or (self.limit.get('five_hour'), self.limit.get('seven_day')) != (new['five_hour'], new['seven_day']):
            self.limit = new
            BUS.publish({'type': 'changed', 'sessions': []})

    def adopt(self, sid, pid, info, started_at=None):
        """A session the holder already runs (e.g. after this server restarted)."""
        if not UUID_RE.match(str(sid or '')):
            return
        directory = os.path.abspath(info.get('dir') or '/')
        if directory not in self.dirs:
            return
        with self.lock:
            s = self.sessions.get(sid)
            if s and s.status != 'closed':
                s.pid = s.pid or pid
                return
            s = ManagedSession(sid, info.get('name'), True, directory, pid)
            s.started_at = started_at
            s.replaying = True
            self.sessions[sid] = s
        log('adopted session from holder %s pid=%s' % (sid[:8], pid))

    def add_dir(self, directory):
        directory = os.path.abspath(directory)
        if directory not in self.dirs:
            self.dirs.append(directory)

    def start(self, sid=None, name=None, directory=None, permission_mode=None, model=None, effort=None, extra_args=None):
        launch = {'permission_mode': permission_mode, 'model': model, 'effort': effort, 'extra_args': extra_args}
        with self.lock:
            if sid:
                if not UUID_RE.match(sid):
                    raise KovanError('invalid session id')
                if self.owned(sid):
                    return self.owned(sid)
                with STORE.lock:
                    live = STORE.live_sessions()
                    STORE.scan(live)
                    t = STORE.transcripts.get(sid)
                    if not t or not os.path.exists(t.path):
                        raise KovanError('no transcript for this session in the watched directories', 404)
                c = live.get(sid)
                if c:
                    where = 'in VS Code' if 'vscode' in (c.get('entrypoint') or '') else 'somewhere else'
                    raise KovanError('This session is open %s (pid %s). Close it there first, then continue in kovan.'
                                    % (where, c.get('pid')), 409)
                s = ManagedSession(sid, None, True, t.directory, **launch)
            else:
                directory = session_directory(directory) if directory else self.dirs[0]
                s = ManagedSession(str(uuid.uuid4()), (name or '').strip()[:80] or None, False, directory, **launch)
            self.sessions[s.sid] = s      # register first so the holder's 'session' event is not adopted twice
            try:
                s.start()
            except Exception:
                self.sessions.pop(s.sid, None)
                raise
            BUS.publish({'type': 'process', 'sid': s.sid, 'state': 'started'})
            return s

    def get(self, sid):
        s = self.owned(sid)
        if not s:
            raise KovanError('This session is not open in kovan.', 409)
        return s


# ============================================================================ globals set in main()
class Config:
    claude_home = os.path.join(HOME, '.claude')
    dirs = []
    notes = []
    port = DEFAULT_PORT
    permission_mode = 'default'
    lang = ''           # UI language when the browser has no choice saved yet ('' = follow the browser)
    claude_bin = None
    demo = False


CFG = Config()
STORE = None
MANAGER = None
HOLDER = None
SETTINGS = None
CREWS = None        # crew.Crews: plan → build → check → review loops over role sessions
SWARM = None        # swarm.Swarm: the swarm engine's API, when --swarm is given
NOTIFY = None       # notify.Notifier: webhook / Telegram messages when a crew needs you
BUS = Bus()
TOKEN = secrets.token_hex(24)
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src data: blob:; base-uri 'none'; form-action 'none'")


def validate_images(items):
    if not isinstance(items, list) or len(items) > 6:
        raise KovanError('at most 6 images')
    out = []
    for i in items:
        if not isinstance(i, dict) or i.get('type') not in IMAGE_TYPES or not isinstance(i.get('data'), str):
            raise KovanError('unsupported image (png, jpeg, gif, webp)')
        try:
            raw = base64.b64decode(i['data'], validate=True)
        except (binascii.Error, ValueError):
            raise KovanError('corrupt image')
        if len(raw) > 5 * 1024 * 1024:
            raise KovanError('image larger than 5 MB')
        out.append({'type': i['type'], 'data': i['data']})
    return out


def valid_sid(sid):
    if not UUID_RE.match(sid or ''):
        raise KovanError('invalid session id')
    return sid


class Handler(BaseHTTPRequestHandler):
    server_version = 'kovan/' + __version__

    def log_message(self, *args):
        pass

    def headers_out(self, status, ctype, length=None):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        self.end_headers()

    def reply(self, status, body, ctype):
        b = body if isinstance(body, bytes) else body.encode('utf-8')
        self.headers_out(status, ctype, len(b))
        self.wfile.write(b)

    def reply_json(self, status, data):
        self.reply(status, json.dumps(data, ensure_ascii=False), 'application/json; charset=utf-8')

    def host_ok(self):
        if self.headers.get('Host') not in self.server.allowed_hosts:
            self.reply(403, 'forbidden', 'text/plain; charset=utf-8')
            return False
        return True

    def do_GET(self):
        if not self.host_ok():
            return
        u = urlparse(self.path)
        if u.path == '/':
            try:      # read on every request: UI changes apply without a restart
                with open(os.path.join(STATIC_DIR, 'index.html'), encoding='utf-8') as f:
                    page = f.read()
            except OSError:
                return self.reply(500, 'index.html missing', 'text/plain; charset=utf-8')
            dirs = ', '.join(tilde(d) for d in CFG.dirs)
            page = page.replace('__TOKEN__', TOKEN).replace('__DIRS__', html.escape(dirs)).replace('__LANG__', CFG.lang)
            return self.reply(200, page, 'text/html; charset=utf-8')
        if not u.path.startswith('/api/'):
            return self.reply(404, 'not found', 'text/plain; charset=utf-8')
        if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
            return self.reply(403, 'forbidden', 'text/plain; charset=utf-8')
        q = parse_qs(u.query)

        def arg(name):
            return q.get(name, [''])[0]
        try:
            if u.path == '/api/health':
                data = {'ok': True, 'version': __version__}
            elif u.path == '/api/events':
                return self.sse()
            elif u.path == '/api/state':
                data = STORE.state(arg('revision'), arg('feed') == '1')
                if not data.get('unchanged'):       # outside STORE.lock: see Crews.reindex
                    data['crews'] = CREWS.list() if CREWS else []
                    data['swarm'] = {'url': SWARM.url, 'error': SWARM.error} if SWARM else None
                    data['notify'] = NOTIFY.state() if NOTIFY else None
            elif u.path == '/api/crew':
                with CREWS.lock:
                    data = CREWS.detail(CREWS.get(arg('id')[:40]))
            elif u.path == '/api/swarm':
                data = {'projects': self.swarm().projects()}
            elif u.path == '/api/swarm/board':
                data = {'entries': self.swarm().board(arg('project'), arg('all') == '1')}
            elif u.path == '/api/session':
                data = STORE.session(valid_sid(arg('id')), arg('revision'))
            elif u.path == '/api/crew/catalog':
                from .crew import catalog
                data = dict(catalog(arg('lang')), saved=SETTINGS.roles() if SETTINGS else [])
            elif u.path == '/api/dirs':
                data = {'dirs': STORE.dir_suggestions()}
            elif u.path == '/api/notes':
                data = {'notes': STORE.read_notes()}
            elif u.path == '/api/tool':
                data = STORE.tool_detail(valid_sid(arg('sid')), arg('tid')[:100])
            elif u.path == '/api/task':
                data = STORE.task_output(valid_sid(arg('sid')), arg('id'))
            elif u.path == '/api/search':
                data = STORE.search(arg('q')[:200])
            else:
                return self.reply(404, 'not found', 'text/plain; charset=utf-8')
        except KovanError as e:
            return self.reply_json(e.status, {'error': str(e)})
        except Exception as e:  # noqa: BLE001
            log('GET error %s: %r' % (u.path, e))
            return self.reply_json(500, {'error': type(e).__name__})
        self.reply_json(200, data)

    @staticmethod
    def swarm():
        if not SWARM:
            raise KovanError('no swarm connected — start kovan with --swarm http://127.0.0.1:8080', 404)
        return SWARM

    def sse(self):
        self.headers_out(200, 'text/event-stream; charset=utf-8')
        q = BUS.subscribe()
        try:
            self.wfile.write(b'retry: 2000\n\n')
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(b'data: ' + data.encode('utf-8') + b'\n\n')
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)

    def do_POST(self):
        if not self.host_ok():
            return
        u = urlparse(self.path)
        if (self.headers.get('Origin') not in self.server.allowed_origins
                or self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin')
                or not hmac.compare_digest(self.headers.get('X-Kovan-Token', ''), TOKEN)):
            log('POST rejected %s (origin/token)' % u.path)
            return self.reply_json(403, {'error': 'forbidden'})
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            return self.reply_json(415, {'error': 'expected JSON'})
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = -1
        if n < 0 or n > 25 * 1024 * 1024:
            return self.reply_json(413, {'error': 'request body too large'})
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.reply_json(400, {'error': 'could not parse request body'})
        try:
            if CFG.demo and u.path not in ('/api/archive', '/api/quick-prompts', '/api/rename', '/api/crew/roles'):
                raise KovanError('Demo mode is read-only. Run kovan without --demo to drive real sessions.', 403)
            data = self.post(u.path, body)
            if data is None:
                return self.reply_json(404, {'error': 'not found'})
        except KovanError as e:
            return self.reply_json(e.status, {'error': str(e)})
        except Exception as e:  # noqa: BLE001
            log('POST error %s: %r' % (u.path, e))
            return self.reply_json(500, {'error': 'server error: %s' % type(e).__name__})
        self.reply_json(200, data)

    def post(self, path, b):
        sid = str(b.get('sid') or '')
        if path == '/api/send':
            text = str(b.get('text') or '')
            if len(text) > 200000:
                raise KovanError('message too long')
            images = validate_images(b.get('images') or [])
            if not text.strip() and not images:
                raise KovanError('empty message')
            MANAGER.get(sid).send(text, images)
            return {'ok': True}
        if path == '/api/takeover':
            return {'ok': True, 'sid': MANAGER.start(sid=sid).sid}
        if path == '/api/new':
            text = str(b.get('text') or '')
            images = validate_images(b.get('images') or [])
            s = MANAGER.start(name=str(b.get('name') or ''), directory=b.get('directory') or None)
            if text.strip() or images:
                s.send(text, images)
            return {'ok': True, 'sid': s.sid}
        if path == '/api/interrupt':
            MANAGER.get(sid).interrupt()
            return {'ok': True}
        if path == '/api/permission':
            answers = b.get('answers')
            if answers is not None and not isinstance(answers, dict):
                raise KovanError('answers must be an object')
            MANAGER.get(sid).answer_permission(str(b.get('id') or ''), 'allow' if b.get('decision') == 'allow' else 'deny',
                                               answers, str(b.get('message') or ''))
            return {'ok': True}
        if path == '/api/release':
            MANAGER.get(sid).release()
            return {'ok': True}
        if path == '/api/model':
            model = str(b.get('model') or '')
            if not MODEL_OK.match(model):
                raise KovanError('invalid model')
            return {'ok': True, 'model': MANAGER.get(sid).set_model(model)}
        if path == '/api/permission-mode':
            mode = str(b.get('mode') or '')
            if mode not in PERMISSION_MODES:
                raise KovanError('invalid permission mode')
            return {'ok': True, 'mode': MANAGER.get(sid).set_permission_mode(mode)}
        if path == '/api/effort':
            effort = str(b.get('effort') or '')
            if effort not in EFFORTS:
                raise KovanError('invalid effort')
            MANAGER.get(sid).send('/effort ' + effort, [])
            return {'ok': True}
        if path == '/api/compact':
            MANAGER.get(sid).send('/compact', [])
            return {'ok': True}
        if path == '/api/rename':
            valid_sid(sid)
            name = re.sub(r'\s+', ' ', str(b.get('name') or '')).strip()[:80]
            s = MANAGER.owned(sid)
            if s and name:
                s.rename(name)
                SETTINGS.set_alias(sid, None)
                return {'ok': True, 'where': 'session'}
            with STORE.lock:
                if sid not in STORE.transcripts:
                    raise KovanError('session not found', 404)
            SETTINGS.set_alias(sid, name or None)
            return {'ok': True, 'where': 'kovan'}
        if path == '/api/rewind':
            msg_uuid = str(b.get('uuid') or '')
            if not UUID_RE.match(msg_uuid):
                raise KovanError('invalid message id')
            return MANAGER.get(sid).rewind(msg_uuid, bool(b.get('dry_run', True)))
        if path == '/api/stop-task':
            task_id = str(b.get('id') or '')
            if not TASK_ID_OK.match(task_id):
                raise KovanError('invalid task id')
            MANAGER.get(sid).stop_task(task_id)
            STORE.mark_task(sid, task_id, 'killed')
            return {'ok': True}
        if path == '/api/archive':
            valid_sid(sid)
            SETTINGS.set_archived(sid, bool(b.get('archived')))
            return {'ok': True}
        if path == '/api/quick-prompts':
            SETTINGS.set_quick_prompts(b.get('items'))
            return {'ok': True}
        if path == '/api/crew/new':
            options = b.get('options') or {}
            if not isinstance(options, dict):
                raise KovanError('options must be an object')
            c = CREWS.create(b.get('name'), b.get('goal'), b.get('theme'), str(b.get('mode') or 'goal'),
                             b.get('directory'), options, template=b.get('template'), team=b.get('team'))
            return {'ok': True, 'id': c['id']}
        if path == '/api/crew/roles':
            SETTINGS.set_roles(b.get('items'))
            return {'ok': True}
        if path == '/api/crew/action':
            index = b.get('index')
            if index is not None and not isinstance(index, int):
                raise KovanError('invalid index')
            return CREWS.act(str(b.get('id') or '')[:40], str(b.get('action') or ''), str(b.get('feedback') or '')[:4000],
                             index, bool(b.get('release')))
        if path == '/api/crew/options':
            return CREWS.update_options(str(b.get('id') or '')[:40], b.get('options'))
        if path == '/api/crew/note':
            role = b.get('role') or None
            return CREWS.note(str(b.get('id') or '')[:40], b.get('text'), str(role) if role else None)
        if path == '/api/swarm/command':
            return self.swarm().command(str(b.get('intent') or ''), b.get('project'), b.get('text'), b.get('role'))
        return None


def watcher():
    previous, previous_offsets = None, {}
    while True:
        time.sleep(1.0)
        try:
            with STORE.lock:
                live = STORE.live_sessions()
                STORE.scan(live)
                rev = STORE.revision(live)
                offsets = {sid: t.offset for sid, t in STORE.transcripts.items()}
            if rev != previous:
                if previous is not None:
                    BUS.publish({'type': 'changed', 'sessions': [sid for sid, v in offsets.items() if previous_offsets.get(sid) != v]})
                previous, previous_offsets = rev, offsets
        except Exception as e:  # noqa: BLE001
            log('watcher error: %r' % e)


# ============================================================================ CLI
def is_running(port):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/api/health' % port, timeout=1.5) as r:
            return r.headers.get('Server', '').startswith('kovan')
    except Exception:  # noqa: BLE001
        return False


def pid_file(port):
    return os.path.join(STATE_DIR, 'kovan-%d.pid' % port)


SOCKET_PATH_MAX = 100      # sun_path is 104 bytes on macOS, 108 on Linux


def holder_socket(port, state_dir=None):
    state_dir = state_dir or STATE_DIR
    path = os.path.join(state_dir, 'holder-%d.sock' % port)
    if len(path.encode('utf-8')) <= SOCKET_PATH_MAX:
        return path
    # A long XDG_STATE_HOME or home directory would overflow the socket path: use a short private directory.
    base = tempfile.gettempdir()
    if len(base) > 48:
        base = '/tmp'
    private = os.path.join(base, 'kovan-%d' % os.getuid())
    os.makedirs(private, mode=0o700, exist_ok=True)
    st = os.lstat(private)
    if not stat_is_private_dir(st):
        raise KovanError('%s is not a private directory owned by you; refusing to put the session socket there' % private, 500)
    return os.path.join(private, 'holder-%s-%d.sock' % (hashlib.sha1(state_dir.encode('utf-8')).hexdigest()[:10], port))


def stat_is_private_dir(st):
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def open_window(url):
    if sys.platform == 'darwin' and os.path.isdir('/Applications/Google Chrome.app'):
        subprocess.Popen(['open', '-na', 'Google Chrome', '--args', '--app=' + url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    for browser in ('google-chrome', 'chromium', 'chromium-browser', 'microsoft-edge'):
        exe = shutil.which(browser)
        if exe:
            subprocess.Popen([exe, '--app=' + url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    webbrowser.open(url)


def close_holder(port):
    client = HolderClient(holder_socket(port))
    s = client._try()
    if not s:
        return False
    try:
        s.sendall(b'{"op":"shutdown"}\n')
        s.settimeout(5)
        s.recv(4096)
    except OSError:
        pass
    finally:
        s.close()
    return True


def serve(args):
    global STORE, MANAGER, HOLDER, SETTINGS, CREWS, SWARM, NOTIFY
    from . import crew, swarm
    CFG.port = args.port
    CFG.permission_mode = args.permission_mode
    CFG.claude_bin = args.claude_bin
    CFG.lang = args.lang or ''
    CFG.demo = args.demo
    task_roots = None
    if args.demo:
        from . import demo
        info = demo.build()
        CFG.claude_home, CFG.dirs, CFG.notes, task_roots = info['claude_home'], info['dirs'], info['notes'], info['task_roots']
        SETTINGS = Settings(os.path.join(info['root'], 'settings.json'), read_only=True)
    else:
        CFG.claude_home = os.path.abspath(os.path.expanduser(args.claude_home))
        CFG.dirs = args.dir
        CFG.notes = [os.path.abspath(os.path.expanduser(n)) for n in args.note]
        SETTINGS = Settings(SETTINGS_PATH)
    STORE = Store(CFG.claude_home, CFG.dirs, CFG.notes, task_roots)
    MANAGER = Manager(CFG.dirs)
    HOLDER = HolderClient(holder_socket(args.port))
    CREWS = crew.Crews(STATE_DIR, read_only=args.demo)
    extra = CREWS.directories() + [d for d in SETTINGS.data.get('dirs', []) if isinstance(d, str) and os.path.isdir(d)]
    for d in extra:     # crew workspaces and folders you started sessions in, before the holder hands sessions back
        STORE.add_dir(d)
        MANAGER.add_dir(d)
    if args.swarm:
        SWARM = swarm.Swarm(args.swarm, os.environ.get('KOVAN_SWARM_TOKEN') or os.environ.get('AIDEVSWARM_API_TOKEN'))
    if args.demo:
        demo.attach(MANAGER, BUS, info)
        demo.attach_crews(CREWS, info)
        SWARM = swarm.DemoSwarm(info['swarm'])
    elif HOLDER.connect(spawn=False):
        log('connected to the session holder; %d session(s) still running' % len(MANAGER.owned_items()))
    t0 = time.time()
    STORE.state('', False)
    log('kovan %s: %d session(s) read in %.1fs from %s; claude: %s' % (
        __version__, len(STORE.transcripts), time.time() - t0, ', '.join(tilde(d) for d in CFG.dirs),
        tilde(find_claude() or 'NOT FOUND') if not args.demo else 'demo'))
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    server.allowed_hosts = {'127.0.0.1:%d' % args.port, 'localhost:%d' % args.port}
    server.allowed_origins = {'http://127.0.0.1:%d' % args.port, 'http://localhost:%d' % args.port}
    threading.Thread(target=watcher, daemon=True, name='watcher').start()
    if not args.demo:
        crew.Runner(CREWS).start()
        from . import notify
        try:
            settings = notify.Settings(getattr(args, 'notify', None), lang=CFG.lang or 'en')
            if settings.enabled:
                NOTIFY = notify.Notifier(settings, CREWS, STORE, MANAGER)
                NOTIFY.start()
        except notify.NotifyError as e:
            log('notifications off: %s' % e)
    if SWARM and not args.demo:
        log('swarm: %s%s' % (SWARM.url, ' (with API token)' if SWARM.token else ''))
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    with open(pid_file(args.port), 'w') as f:
        f.write(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # sessions keep running in the holder
    log('ready: http://127.0.0.1:%d' % args.port)
    try:
        server.serve_forever()
    finally:
        try:
            os.remove(pid_file(args.port))
        except OSError:
            pass


MINIMAL_PATH = {'/usr/bin', '/bin', '/usr/sbin', '/sbin'}


def login_path(environ=None, runner=subprocess.run):
    """A PATH for sessions when kovan was started from the Dock or launchd, which only pass /usr/bin:/bin….

    Asks the user's login shell for its PATH (Homebrew, ~/.local/bin, nvm…). Returns None when the current PATH
    is already a real one or the shell does not answer."""
    environ = os.environ if environ is None else environ
    if set(filter(None, environ.get('PATH', '').split(':'))) - MINIMAL_PATH:
        return None
    shell = environ.get('SHELL') or '/bin/sh'
    try:
        r = runner([shell, '-l', '-c', 'printf %s "$PATH"'], capture_output=True, text=True, timeout=8,
                   stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = [x for x in (r.stdout or '').strip().split(':') if x] if r.returncode == 0 else []
    if '/usr/bin' not in parts:
        return None
    local = os.path.join(environ.get('HOME') or HOME, '.local', 'bin')
    if local not in parts and os.path.isdir(local):
        parts.append(local)
    return ':'.join(parts)


def load_config():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def build_parser(defaults):
    p = argparse.ArgumentParser(prog='kovan', description='A local control room for parallel Claude Code sessions.')
    p.add_argument('--dir', action='append', default=None, metavar='PATH',
                   help='project directory whose sessions to show (repeatable; default: current directory)')
    p.add_argument('--note', action='append', default=None, metavar='FILE',
                   help='markdown file to show in the Notes tab, e.g. a journal or TODO (repeatable)')
    p.add_argument('--port', type=int, default=None, help='port on 127.0.0.1 (default %d, demo %d)' % (DEFAULT_PORT, DEMO_PORT))
    p.add_argument('--permission-mode', choices=PERMISSION_MODES, default=defaults.get('permission_mode', 'default'),
                   help='permission mode for sessions kovan starts (default: default = ask before acting)')
    p.add_argument('--claude-bin', default=defaults.get('claude_bin'), help='path to the claude executable')
    p.add_argument('--lang', choices=['en', 'tr'], default=defaults.get('lang') if defaults.get('lang') in ('en', 'tr') else None,
                   help='UI language until you pick one in the page (default: follow the browser)')
    p.add_argument('--claude-home', default=defaults.get('claude_home') or os.environ.get('KOVAN_CLAUDE_HOME') or
                   os.path.join(HOME, '.claude'), help=argparse.SUPPRESS)
    p.add_argument('--swarm', default=defaults.get('swarm'), metavar='URL',
                   help='show the boardroom of a running swarm engine, e.g. http://127.0.0.1:8080 (loopback only; '
                        'token from KOVAN_SWARM_TOKEN)')
    p.add_argument('--demo', action='store_true', help='run with synthetic sessions (read-only; nothing real is touched)')
    p.add_argument('--window', action='store_true', help='open in a chromeless browser window')
    p.add_argument('--no-browser', action='store_true', help='do not open a browser')
    p.add_argument('--foreground', action='store_true', help='run in this terminal instead of in the background')
    p.add_argument('--stop', action='store_true', help='stop the web server (sessions keep running in the holder)')
    p.add_argument('--stop-all', action='store_true', help='release kovan sessions, stop the holder and the web server')
    p.add_argument('--notify-test', action='store_true', help='send a test message to every configured notification channel (Claude, webhook, Telegram) and exit')
    p.add_argument('--version', action='version', version='kovan ' + __version__)
    return p


def main(argv=None):
    path = login_path()
    if path:
        os.environ['PATH'] = path
    defaults = load_config()
    args = build_parser(defaults).parse_args(argv)
    args.port = args.port or (DEMO_PORT if args.demo else defaults.get('port', DEFAULT_PORT))
    args.dir = [os.path.abspath(os.path.expanduser(d)) for d in (args.dir or defaults.get('dirs') or [os.getcwd()])]
    args.note = args.note or defaults.get('notes') or []
    if args.swarm:
        from .swarm import check_url
        try:
            args.swarm = check_url(args.swarm)
        except ValueError as e:
            sys.exit('kovan: %s' % e)
    args.notify = defaults.get('notify')
    url = 'http://127.0.0.1:%d' % args.port

    if args.notify_test:
        from .notify import NotifyError, Settings, send_test
        try:
            settings = Settings(args.notify, lang=args.lang or 'en')
        except NotifyError as e:
            sys.exit('kovan: %s' % e)
        if not settings.enabled:
            sys.exit('kovan: no notification channel configured — see "notify" in the README')
        for channel, result in send_test(settings).items():
            print('%s: %s' % (channel, result))
        return
    if args.stop or args.stop_all:
        if args.stop_all:
            print('kovan sessions released' if close_holder(args.port) else 'holder was not running')
        try:
            with open(pid_file(args.port)) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            print('kovan is not running on port %d' % args.port)
            return
        if is_running(args.port):
            os.kill(pid, signal.SIGTERM)
            holder_up = os.path.exists(holder_socket(args.port))
            print('kovan stopped (pid %d)%s' % (pid, '; kovan sessions keep running in the holder'
                                                    if holder_up and not args.stop_all else ''))
        else:
            print('stale pid file, left alone (pid %d)' % pid)
        return
    if args.foreground:
        return serve(args)

    if not is_running(args.port):
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        logf = open(os.path.join(STATE_DIR, 'kovan.log'), 'ab')
        cmd = [sys.executable, '-m', 'kovan', '--foreground', '--port', str(args.port),
               '--permission-mode', args.permission_mode, '--claude-home', args.claude_home]
        for d in args.dir:
            cmd += ['--dir', d]
        for n in args.note:
            cmd += ['--note', os.path.abspath(os.path.expanduser(n))]
        if args.claude_bin:
            cmd += ['--claude-bin', args.claude_bin]
        if args.swarm:
            cmd += ['--swarm', args.swarm]
        if args.lang:
            cmd += ['--lang', args.lang]
        if args.demo:
            cmd.append('--demo')
        subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                         env=child_env())
        for _ in range(240):
            if is_running(args.port):
                break
            time.sleep(0.5)
        else:
            print('kovan did not start; see %s' % os.path.join(STATE_DIR, 'kovan.log'))
            sys.exit(1)
    print(url)
    if args.window:
        open_window(url)
    elif not args.no_browser:
        webbrowser.open(url)
