"""Crews: plan → build → check → review loops run by role sessions that kovan drives.

A crew turns a goal — yours, or one its Ideator proposes — into small milestones and works through
them with role sessions: Planner, Developer, an optional Tester, and Reviewer. They are ordinary
Claude Code sessions driven by kovan, so you can open any of them and talk to it. Between the roles
sits kovan's orchestrator: it runs your check command (the CI gate), commits every accepted milestone,
keeps a boardroom log of each hand-off and decision, pauses when the usage limit gets close, and asks
you whenever a decision or a risky command needs a human.

The design is ported from the swarm engine in this repository (src/aidevswarm): the milestone graph,
the role charters, the replanner's continue / amend / split / escalate actions, the retry budget,
steering notes and the finance officer — rebuilt on Claude Code sessions instead of CrewAI + the API.
"""
import json
import os
import re
import secrets
import subprocess
import threading
import time

from . import server

ROLES = ('ideator', 'planner', 'developer', 'tester', 'reviewer')
TITLES = {'ideator': 'Ideator', 'planner': 'Planner', 'developer': 'Developer', 'tester': 'Tester',
          'reviewer': 'Reviewer', 'finance': 'Finance', 'kovan': 'Kovan', 'you': 'You'}
WORKING = ('ideating', 'planning', 'building')
WAITING_ON_YOU = ('choosing', 'plan_review', 'milestone_review', 'blocked')
FINISHED = ('done', 'stopped')
DEFAULT_OPTIONS = {'tester': False, 'plan_approval': True, 'milestone_approval': False, 'policy': 'safe',
                   'max_attempts': 3, 'quota_pause': 90, 'check': '', 'check_timeout': 900, 'commit': True,
                   'push': False}
BOARD_LIMIT = 800
REPLY_TIMEOUT = 3 * 3600

CONTRACT = ('\n\nEnd your reply with a single JSON object in a ```json fenced block, exactly in the shape '
            'described above. Write any explanation before it.')

CHARTERS = {
    'ideator': (
        "You are the Ideator of a small autonomous software crew. You propose concrete, ambitious but buildable "
        "projects and critique your own ideas honestly before anyone spends effort on them.\n"
        "Rules: no trivial CLIs, scrapers or thin wrappers around one API; buildable without paid accounts beyond "
        "Claude and GitHub; decomposable into 3–8 milestones of a few hours each. Use web search to check that an "
        "idea is not already a well-known project, and say what you found.\n"
        "Score each idea 0–100 on: depth (30%), usefulness for a real niche (25%), novelty (20%), "
        "decomposability (15%), buildability (10%)."),
    'planner': (
        "You are the Planner of a small autonomous software crew (product manager and architect in one). You turn a "
        "goal into an ORDERED list of small, independently testable milestones and set the technical foundation: "
        "stack, layout, and the command that proves the work (tests/lint).\n"
        "Rules: 3–8 milestones, smaller is better (a milestone that takes a day is too big); every milestone has "
        "concrete acceptance criteria a reviewer can verify; prefer boring, well-supported tools; no steps that "
        "need a human; read the workspace before planning — never plan to delete work that already exists.\n"
        "Between milestones you are also the Replanner: you look at what just happened and decide whether to "
        "continue, tighten (amend) or split what comes next, or escalate to the human."),
    'developer': (
        "You are the Developer of a small autonomous software crew. You receive ONE milestone at a time and "
        "implement it in the current workspace.\n"
        "Process: read the existing code first (never delete earlier milestones' work); make the smallest correct "
        "change; add or update tests for the acceptance criteria; run the check command yourself and fix what "
        "fails. Do not commit — the orchestrator commits after review. Never write secrets or .env files, never "
        "run destructive commands, and stay inside this workspace.\n"
        "Commands run without waiting for a human only when they are simple: the check command, test runners, "
        "linters and read-only git, with relative paths and no pipes, redirection or $(...). Prefer those."),
    'tester': (
        "You are the Tester of a small autonomous software crew. After the Developer finishes a milestone you add "
        "or strengthen tests so that every acceptance criterion is genuinely exercised, then run them.\n"
        "Never weaken, skip or delete tests. Do not fix application code yourself — report a concrete, minimal "
        "reproduction so the Developer can fix it. Run tests with simple commands (no pipes or redirection) so "
        "they do not wait for a human."),
    'reviewer': (
        "You are the Reviewer of a small autonomous software crew. You decide whether a milestone is really done.\n"
        "Verify each acceptance criterion against the actual diff — not 'tests pass so it must be fine'. Check for "
        "dead code, premature abstractions, leaked secrets, broken existing behaviour and missing tests. Approve "
        "only work you would merge. If you request changes, give a short, prioritised list the Developer can act on.\n"
        "Verify by reading the diff, the code and the tests, and by running the check command. Ad-hoc scripts "
        "(python -c, pipes, network) wait for a human's OK — if behaviour is unproven, ask for a test instead."),
}

# File tools are allowed only inside the crew's workspace: in acceptEdits mode Claude Code asks about exactly
# the paths *outside* it, so "allow every Read" would hand out ~/.ssh.
FILE_TOOLS = {'Read': 'file_path', 'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path',
              'NotebookEdit': 'notebook_path', 'NotebookRead': 'notebook_path', 'Glob': 'path', 'Grep': 'path', 'LS': 'path'}
FREE_TOOLS = {'TodoWrite', 'ToolSearch', 'Task', 'Agent'}     # subagents' own tool calls still come through here
WEB_TOOLS = {'WebSearch', 'WebFetch'}
SECRET_FILE = re.compile(r'(^|/)(\.env(\..*)?|.*\.pem|.*\.key|id_(rsa|ed25519|ecdsa)(\.pub)?|credentials\.json|'
                         r'\.npmrc|\.pypirc|\.netrc|secrets?\.(ya?ml|json|toml))$', re.I)
SAFE_COMMAND = re.compile(
    r'^(?:(?:python3?|py)(?:\s+-[BqWu]\S*)*\s+-m\s+(?:pytest|unittest|py_compile|compileall|mypy|ruff|black|isort|'
    r'flake8|pyflakes|doctest)\b|pytest\b|ruff\b|mypy\b|black\s+--check\b|flake8\b|'
    r'npm\s+(?:test|run\s+(?:test|lint|build|typecheck|check))\b|npx\s+(?:tsc|eslint|vitest|jest|prettier\s+--check)\b|'
    r'node\s+--check\b|go\s+(?:test|build|vet)\b|cargo\s+(?:test|build|check|clippy)\b|make\s+(?:test|check|lint)\b|'
    r'git\s+(?:status|diff|log|show|rev-parse|ls-files|branch\s+--show-current)\b|ls\b|cat\b|head\b|tail\b|wc\b|pwd\b|echo\b|'
    r'mkdir\b|touch\b|which\b|tree\b)')
# Harmless tails Claude habitually appends to a test run; stripped (from the end only) before the checks below.
BENIGN_TAILS = (re.compile(r'\s*;\s*echo\s+(["\']?)[\w ]*[=:]?\s*\$\?\1\s*$'),     # ; echo "exit=$?"
                re.compile(r'\s*\|\s*(?:tail|head)(?:\s+-n)?(?:\s+-?\d+)?\s*$'),      # | tail -20
                re.compile(r'\s*2>&1\s*$'))                                                # 2>&1
FORBIDDEN = re.compile(r'[|`$<>&]|\brm\b|\bsudo\b|\bcurl\b|\bwget\b|\bssh\b|\bscp\b|\bchmod\b|\bchown\b|'
                       r'-exec\b|-delete\b|--force\b|\bpip\s+install\b')


# ============================================================================ helpers
def now_ms():
    return int(time.time() * 1000)


def extract_json(text):
    """The last JSON object in a reply: a ```json block if present, else the last balanced {...}."""
    text = text or ''
    for block in reversed(re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.S)):
        try:
            return json.loads(block)
        except ValueError:
            continue
    depth, start, last = 0, None, None
    in_str, esc = False, False
    for i, ch in enumerate(text):
        if in_str:
            esc = (ch == '\\') and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}' and depth:
            depth -= 1
            if depth == 0 and start is not None:
                last = text[start:i + 1]
    if last:
        try:
            return json.loads(last)
        except ValueError:
            return None
    return None


def inside(root, path):
    if not root:
        return False
    root = os.path.realpath(root)
    target = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
    return target == root or target.startswith(root + os.sep)


def strip_benign(command):
    while True:
        before = command
        for tail in BENIGN_TAILS:
            command = tail.sub('', command)
        if command == before:
            return command


def climbs(token):
    """Does a path-ish token leave its directory (a '..' component)? './...' (Go's 'all packages') does not."""
    return any(part == '..' for part in re.split(r'[/=:]', token))


def auto_allow(policy, role, tool, inp, check_command='', root=None):
    """Should a crew answer this permission request by itself? Anything not clearly safe goes to a human."""
    if policy == 'ask':
        return False
    inp = inp if isinstance(inp, dict) else {}
    if tool in FILE_TOOLS:
        path = inp.get(FILE_TOOLS[tool])
        return inside(root, str(path)) if path else tool in ('Glob', 'Grep', 'LS')
    if tool in FREE_TOOLS:
        return True
    if tool in WEB_TOOLS:
        return role in ('ideator', 'planner')
    if tool != 'Bash':
        return False
    command = str((inp or {}).get('command') or '').strip()
    if not command or '\n' in command:
        return False
    if check_command and command == check_command.strip():
        return True
    command = strip_benign(command)
    if not command or (check_command and command == check_command.strip()):
        return bool(command)
    for part in re.split(r'\s*(?:&&|;)\s*', command):
        if not part:
            return False
        tokens = part.split()
        if tokens[0] == 'cd':
            if len(tokens) != 2 or climbs(tokens[1]) or tokens[1].startswith(('/', '~')):
                return False
            continue
        if FORBIDDEN.search(part) or not SAFE_COMMAND.match(part):
            return False
        if any(t.startswith(('/', '~')) or climbs(t) for t in tokens[1:]):
            return False
    return True


def attention_summary(tool, inp):
    """What exactly you are asked to allow: the command or path, not just the tool's name."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == 'Bash' and inp.get('command'):
        return server.mask_secrets('$ ' + server.clip(str(inp['command']), 400))[0]
    if tool in FILE_TOOLS and inp.get(FILE_TOOLS[tool]):
        return '%s %s' % (tool, server.tilde(str(inp[FILE_TOOLS[tool]])))
    if tool in WEB_TOOLS:
        return '%s %s' % (tool, server.clip(str(inp.get('url') or inp.get('query') or ''), 200))
    return server.tool_label(tool, inp)


def git(directory, *args, timeout=60):
    try:
        r = subprocess.run(['git', *args], cwd=directory, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def is_git_repo(directory):
    return os.path.isdir(directory) and git(directory, 'rev-parse', '--is-inside-work-tree')[0] == 0


def stage(directory):
    """git add -A, then take secret-looking files back out of the index. Returns the paths it held back."""
    git(directory, 'add', '-A')
    names = git(directory, 'diff', '--cached', '--name-only', '-z')[1].split('\0')
    held = [n for n in names if n and SECRET_FILE.search(n)]
    if held:
        git(directory, 'reset', '-q', '--', *held)
    return held


def check_env():
    """The check command runs like a plain shell: no Claude Code or kovan variables."""
    return {k: v for k, v in server.session_env().items() if not k.startswith(('CLAUDE', 'MCP_'))}


def workspace_listing(directory, limit=60):
    try:
        names = sorted(n for n in os.listdir(directory) if n != '.git')
    except OSError:
        return '(unreadable)'
    shown = [n + ('/' if os.path.isdir(os.path.join(directory, n)) else '') for n in names[:limit]]
    more = '' if len(names) <= limit else '\n… and %d more' % (len(names) - limit)
    return ('\n'.join(shown) or '(empty)') + more


def slug(text):
    s = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return s[:40] or 'crew'


def milestones_text(items, start=1):
    out = []
    for i, m in enumerate(items, start):
        acc = '\n'.join('   - ' + a for a in (m.get('acceptance') or []))
        out.append('%d. %s — %s%s' % (i, m.get('title', ''), m.get('goal', ''), ('\n' + acc) if acc else ''))
    return '\n'.join(out) or '(none)'


def clean_milestones(items):
    out = []
    for m in items or []:
        if not isinstance(m, dict) or not str(m.get('title') or '').strip():
            continue
        out.append({'id': 'm' + secrets.token_hex(3), 'title': str(m['title']).strip()[:120],
                    'goal': str(m.get('goal') or m.get('description') or '').strip()[:2000],
                    'acceptance': [str(a).strip()[:400] for a in (m.get('acceptance') or m.get('acceptance_criteria') or [])
                                   if str(a).strip()][:12],
                    'status': 'pending', 'attempts': 0, 'commit': None, 'log': []})
    return out[:12]


# ============================================================================ the store
class Crews:
    """Crews persisted as JSON files under <state dir>/crews/, plus the orchestrator loop."""

    def __init__(self, state_dir, read_only=False):
        self.dir = os.path.join(state_dir, 'crews')
        self.read_only = read_only
        self.items = {}
        self.lock = threading.RLock()
        self.revision = 0
        self.busy = {}          # crew id -> background operation name (check, commit…)
        self.role_index = {}
        if not read_only and os.path.isdir(self.dir):
            for name in sorted(os.listdir(self.dir)):
                if name.endswith('.json'):
                    try:
                        with open(os.path.join(self.dir, name), encoding='utf-8') as f:
                            crew = json.load(f)
                        if isinstance(crew, dict) and crew.get('id'):
                            self.items[crew['id']] = crew
                    except (OSError, ValueError):
                        server.log('could not read crew %s' % name)
        self.reindex()

    # ---- persistence
    def save(self, crew):
        crew['updated_at'] = now_ms()
        self.revision += 1
        self.reindex()
        server.BUS.publish({'type': 'crew', 'id': crew['id']})
        if self.read_only:
            return
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.dir, crew['id'] + '.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(crew, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def directories(self):
        return sorted({c['dir'] for c in self.items.values() if c.get('dir')})

    def reindex(self):
        # Replaced wholesale (an atomic assignment): Store reads it while holding its own lock, and must never
        # wait for self.lock — the runner holds self.lock while it starts sessions, which takes Store's lock.
        self.role_index = {sid: {'id': c['id'], 'name': c['name'], 'role': role}
                           for c in list(self.items.values()) for role, sid in (c.get('roles') or {}).items()}

    def role_of(self, sid):
        return self.role_index.get(sid)

    def get(self, crew_id):
        crew = self.items.get(crew_id)
        if not crew:
            raise server.KovanError('crew not found', 404)
        return crew

    def say(self, crew, speaker, text, kind='status', to=None, milestone=None, save=True):
        board = crew.setdefault('board', [])
        board.append({'id': secrets.token_hex(4), 'ts': now_ms(), 'speaker': speaker, 'to': to, 'kind': kind,
                      'text': server.clip(str(text), 6000), 'milestone': milestone})
        del board[:-BOARD_LIMIT]
        if save:
            self.save(crew)

    # ---- views
    def summary(self, crew):
        plan = (crew.get('plan') or {}).get('milestones') or []
        done = sum(1 for m in plan if m['status'] in ('done', 'skipped'))
        cur = plan[crew['current']] if plan and 0 <= crew.get('current', 0) < len(plan) else None
        last = (crew.get('board') or [None])[-1]
        return {'id': crew['id'], 'name': crew['name'], 'goal': crew.get('goal') or '', 'theme': crew.get('theme') or '',
                'mode': crew.get('mode'),
                'phase': crew['phase'], 'reason': crew.get('reason'), 'dir': server.tilde(crew['dir']),
                'progress': {'done': done, 'total': len(plan)},
                'current': {'title': cur['title'], 'step': crew.get('step'), 'attempts': cur['attempts']} if cur else None,
                'attention': crew.get('attention'), 'roles': crew.get('roles') or {}, 'options': crew['options'],
                'created_at': crew['created_at'], 'updated_at': crew.get('updated_at'), 'busy': self.busy.get(crew['id']),
                'last': last}

    def detail(self, crew):
        out = self.summary(crew)
        out.update({'plan': crew.get('plan'), 'ideas': crew.get('ideas') or [], 'board': crew.get('board') or [],
                    'notes': crew.get('notes') or [], 'theme': crew.get('theme') or '',
                    'wait': {k: v for k, v in (crew.get('wait') or {}).items() if k != 'prompt'} or None})
        return out

    def list(self):
        with self.lock:
            return sorted((self.summary(c) for c in self.items.values()), key=lambda c: -(c['updated_at'] or 0))

    # ---- creation and human actions
    def create(self, name, goal, theme, mode, directory, options):
        name = re.sub(r'\s+', ' ', str(name or '')).strip()[:60]
        if not name:
            raise server.KovanError('a crew needs a name')
        if mode not in ('goal', 'ideas'):
            raise server.KovanError('mode must be goal or ideas')
        goal = str(goal or '').strip()[:6000]
        if mode == 'goal' and not goal:
            raise server.KovanError('describe the goal, or let the crew suggest ideas')
        if not str(directory or '').strip():
            raise server.KovanError('choose a workspace directory')
        directory = os.path.abspath(os.path.expanduser(str(directory).strip()))
        if directory in ('/', server.HOME) or server.HOME.startswith(directory + os.sep):
            raise server.KovanError('use a project folder, not %s' % server.tilde(directory))
        opts = dict(DEFAULT_OPTIONS)
        try:
            for k, v in (options or {}).items():
                if k in opts:
                    opts[k] = bool(v) if isinstance(DEFAULT_OPTIONS[k], bool) else type(DEFAULT_OPTIONS[k])(v)
        except (TypeError, ValueError):
            raise server.KovanError('invalid crew options')
        if opts['policy'] not in ('ask', 'safe', 'trusted'):
            raise server.KovanError('policy must be ask, safe or trusted')
        opts['max_attempts'] = max(1, min(opts['max_attempts'], 10))
        opts['quota_pause'] = max(50, min(opts['quota_pause'], 100))
        opts['check_timeout'] = max(30, min(opts['check_timeout'], 7200))
        opts['check'] = opts['check'].strip()[:500]
        if any(c.get('dir') == directory and c['phase'] not in FINISHED for c in self.items.values()):
            raise server.KovanError('another crew is already working in that folder')
        if os.path.isdir(directory) and not is_git_repo(directory) and workspace_listing(directory) != '(empty)':
            if opts['commit']:
                raise server.KovanError('that folder has files but is not a git repository — choose an empty folder or '
                                        'a git repository, so every milestone is a reviewable commit')
        if is_git_repo(directory) and git(directory, 'status', '--porcelain')[1].strip():
            raise server.KovanError('that repository has uncommitted changes — commit or stash them first, so the crew’s '
                                    'commits contain only its own work')
        os.makedirs(directory, exist_ok=True)
        if opts['commit'] and not is_git_repo(directory):
            git(directory, 'init', '-q', '-b', 'main')
        crew = {'id': 'c' + secrets.token_hex(4), 'name': name, 'goal': goal, 'theme': str(theme or '').strip()[:2000],
                'mode': mode, 'dir': directory, 'options': opts, 'created_at': now_ms(),
                'phase': 'ideating' if mode == 'ideas' else 'planning', 'reason': None, 'roles': {}, 'primed': {},
                'plan': None, 'ideas': [], 'current': 0, 'step': None, 'wait': None, 'feedback': None,
                'plan_feedback': None, 'notes': [], 'board': [], 'attention': None, 'resume_at': None,
                'paused_from': None, 'stats': {'turns': 0, 'checks': 0}}
        with self.lock:
            self.items[crew['id']] = crew
            server.STORE.add_dir(directory)
            server.MANAGER.add_dir(directory)
            self.say(crew, 'you', goal if mode == 'goal' else 'Suggest projects%s.' % (': ' + crew['theme'] if crew['theme'] else ''),
                     kind='goal', save=False)
            self.say(crew, 'kovan', 'Crew created in %s. %s' % (
                server.tilde(directory), 'The Ideator goes first.' if mode == 'ideas' else 'The Planner goes first.'))
        server.log('action: crew created %s (%s)' % (crew['id'], mode))
        return crew

    def act(self, crew_id, action, feedback='', index=None, release=False):
        with self.lock:
            crew = self.get(crew_id)
            phase = crew['phase']
            if action == 'approve' and phase == 'plan_review':
                proposed = crew['plan'].get('check_command')
                if not crew['options']['check'] and proposed:
                    crew['options']['check'] = proposed     # you saw it on the plan card
                crew.update(phase='building', step='develop', current=self.next_pending(crew), reason=None)
                self.say(crew, 'you', 'Plan approved — go ahead.', kind='decision')
            elif action == 'revise' and phase == 'plan_review':
                crew.update(phase='planning', plan_feedback=str(feedback)[:4000], reason=None)
                self.say(crew, 'you', 'Please revise the plan: ' + str(feedback), kind='decision', to='planner')
            elif action == 'pick' and phase == 'choosing':
                ideas = crew.get('ideas') or []
                if index is None or not 0 <= int(index) < len(ideas):
                    raise server.KovanError('pick one of the ideas')
                idea = ideas[int(index)]
                crew['goal'] = '%s — %s' % (idea.get('title', ''), idea.get('summary', ''))
                crew.update(phase='planning', reason=None)
                self.say(crew, 'you', 'Let’s build “%s”.' % idea.get('title', ''), kind='decision')
            elif action == 'more_ideas' and phase == 'choosing':
                crew.update(phase='ideating', reason=None)
                self.say(crew, 'you', 'More ideas, please.' + (' ' + str(feedback) if feedback else ''), kind='decision', to='ideator')
                if feedback:
                    crew['theme'] = (crew.get('theme', '') + '\nAlso: ' + str(feedback)[:1000]).strip()
            elif action == 'continue' and phase == 'milestone_review':
                crew.update(phase='building', step='develop', current=self.next_pending(crew), reason=None)
                self.say(crew, 'you', 'Continue with the next milestone.', kind='decision')
            elif action == 'retry' and phase == 'blocked':
                self.interrupt_active(crew)
                crew['wait'] = None
                plan = (crew.get('plan') or {}).get('milestones') or []
                if crew.get('plan') and plan:
                    cur = plan[crew['current']]
                    cur['attempts'] = 0
                    cur['status'] = 'active'
                    crew.update(phase='building', step='develop', reason=None, attention=None)
                else:
                    crew.update(phase='ideating' if crew['mode'] == 'ideas' and not crew.get('goal') else 'planning',
                                reason=None, attention=None)
                self.say(crew, 'you', 'Try again.' + (' ' + str(feedback) if feedback else ''), kind='decision')
                if feedback:
                    crew['feedback'] = str(feedback)[:4000]
            elif action == 'skip' and phase in ('blocked', 'building', 'milestone_review') and crew.get('plan') \
                    and crew['current'] < len(crew['plan']['milestones']):
                if crew['id'] in self.busy:
                    raise server.KovanError('wait a moment — kovan is running the %s step' % self.busy[crew['id']], 409)
                self.interrupt_active(crew)
                crew.update(wait=None, attention=None, reason=None, phase='building')
                plan = crew['plan']['milestones']
                plan[crew['current']]['status'] = 'skipped'
                self.say(crew, 'you', 'Skip “%s”.' % plan[crew['current']]['title'], kind='decision', save=False)
                self.advance_milestone(crew)
            elif action == 'pause' and phase not in FINISHED + ('paused',):
                crew.update(paused_from=phase, phase='paused', reason='paused by you', resume_at=None)
                self.say(crew, 'you', 'Pause.', kind='decision')
            elif action == 'resume' and phase == 'paused':
                crew.update(phase=crew.get('paused_from') or 'planning', reason=None, resume_at=None, paused_from=None)
                self.say(crew, 'you', 'Resume.', kind='decision')
            elif action == 'stop' and phase not in FINISHED:
                self.interrupt_active(crew)
                crew.update(phase='stopped', reason='stopped by you', wait=None)
                self.say(crew, 'you', 'Stop the crew.', kind='decision')
            elif action == 'delete':
                if phase in WORKING and crew.get('wait'):
                    self.interrupt_active(crew)
                if release:
                    for sid in (crew.get('roles') or {}).values():
                        s = server.MANAGER.owned(sid)
                        if s:
                            try:
                                s.release()
                            except server.KovanError:
                                pass
                self.items.pop(crew_id, None)
                self.revision += 1
                self.reindex()
                if not self.read_only:
                    try:
                        os.remove(os.path.join(self.dir, crew_id + '.json'))
                    except OSError:
                        pass
                server.BUS.publish({'type': 'crew', 'id': crew_id})
                server.log('action: crew deleted %s' % crew_id)
                return {'ok': True}
            else:
                raise server.KovanError('“%s” is not possible while the crew is %s' % (action, phase.replace('_', ' ')), 409)
            server.log('action: crew %s %s' % (crew_id, action))
            return self.detail(crew)

    def note(self, crew_id, text, role=None):
        text = str(text or '').strip()[:4000]
        if not text:
            raise server.KovanError('empty note')
        if role and role not in ROLES:
            raise server.KovanError('unknown role')
        with self.lock:
            crew = self.get(crew_id)
            note = {'ts': now_ms(), 'text': text, 'role': role, 'delivered': False}
            wait = crew.get('wait') or {}
            target = role or wait.get('role')
            s = server.MANAGER.owned((crew.get('roles') or {}).get(target)) if target else None
            if s and (not role or role == wait.get('role')) and s.status == 'working':
                # The session is mid-turn: Claude Code absorbs a queued message into the running turn.
                s.send('Operator note: ' + text, [])
                note['delivered'] = True
            crew.setdefault('notes', []).append(note)
            self.say(crew, 'you', text, kind='note', to=role)
        return {'ok': True, 'delivered': note['delivered']}

    # ---- orchestration
    def next_pending(self, crew):
        for i, m in enumerate((crew.get('plan') or {}).get('milestones') or []):
            if m['status'] in ('pending', 'active', 'failed'):
                return i
        return len((crew.get('plan') or {}).get('milestones') or [])

    def interrupt_active(self, crew):
        wait = crew.get('wait') or {}
        s = server.MANAGER.owned((crew.get('roles') or {}).get(wait.get('role'))) if wait else None
        if s and s.status == 'working':
            try:
                s.interrupt()
            except server.KovanError:
                pass

    def pending_notes(self, crew, role):
        out = []
        for n in crew.get('notes') or []:
            if not n['delivered'] and (n.get('role') in (None, role)):
                out.append(n['text'])
                n['delivered'] = True
        return ('\n\nNotes from the operator (follow them):\n' + '\n'.join('- ' + t for t in out)) if out else ''

    def role_session(self, crew, role):
        sid = (crew.get('roles') or {}).get(role)
        s = server.MANAGER.owned(sid) if sid else None
        if s:
            return s
        mode = 'bypassPermissions' if crew['options']['policy'] == 'trusted' else 'acceptEdits'
        if sid:
            s = server.MANAGER.start(sid=sid, permission_mode=mode)
            self.say(crew, 'kovan', '%s session resumed.' % TITLES[role], save=False)
        else:
            s = server.MANAGER.start(name='%s · %s' % (crew['name'], TITLES[role]), directory=crew['dir'],
                                     permission_mode=mode)
            crew.setdefault('roles', {})[role] = s.sid
            crew.setdefault('primed', {})[role] = False
            self.say(crew, 'kovan', '%s joined (a new Claude Code session).' % TITLES[role], save=False)
        return s

    def ask(self, crew, role, step, task, to=None):
        """Send a task to a role. Returns False when the role is busy (try again on the next tick)."""
        if self.quota_exceeded(crew):
            return False
        s = self.role_session(crew, role)
        if s.status == 'working' or s.permissions:
            return False
        text = task + self.pending_notes(crew, role) + CONTRACT
        if not crew.setdefault('primed', {}).get(role):
            text = CHARTERS[role] + '\n\n---\n\n' + text
        crew['wait'] = {'role': role, 'step': step, 'sent_at': time.time(), 'retries': 0, 'prompt': text}
        crew['stats']['turns'] = crew['stats'].get('turns', 0) + 1
        s.send(text, [])
        crew['primed'][role] = True
        self.save(crew)
        return True

    def quota_exceeded(self, crew):
        limit = (server.MANAGER.limit or {}).get('five_hour') if server.MANAGER else None
        if not limit or limit.get('percent') is None or limit['percent'] < crew['options']['quota_pause']:
            return False
        resume_at = limit.get('resets_at')
        crew.update(paused_from=crew['phase'], phase='paused', reason='usage limit',
                    resume_at=int(resume_at * 1000) if resume_at else None)
        self.say(crew, 'finance', 'Pausing: the 5-hour usage window is at %d%% (limit %d%%).%s' % (
            limit['percent'], crew['options']['quota_pause'],
            ' I will resume at %s.' % time.strftime('%H:%M', time.localtime(resume_at)) if resume_at else ''), kind='finance')
        return True

    def finance_report(self, crew):
        lim = server.MANAGER.limit if server.MANAGER else None
        parts = ['%d role turns so far' % crew['stats'].get('turns', 0)]
        if lim:
            for key, label in (('five_hour', '5-hour'), ('seven_day', '7-day')):
                w = lim.get(key) or {}
                if w.get('percent') is not None:
                    parts.append('%s usage %d%%' % (label, w['percent']))
        self.say(crew, 'finance', '; '.join(parts) + '.', kind='finance', save=False)

    def tick(self):
        with self.lock:
            crews = list(self.items.values())
        for crew in crews:
            if crew['id'] in self.busy:
                continue
            try:
                with self.lock:
                    self.step(crew)
            except server.KovanError as e:
                self.block(crew, str(e))
            except Exception as e:  # noqa: BLE001 - one broken crew must not stop the others
                server.log('crew %s error: %r' % (crew['id'], e))
                self.block(crew, 'internal error: %s' % type(e).__name__)

    def block(self, crew, reason):
        with self.lock:
            crew.update(phase='blocked', reason=reason)
            self.say(crew, 'kovan', 'Blocked: ' + reason + ' — retry, skip or stop when you are ready.', kind='blocked')

    def step(self, crew):
        phase = crew['phase']
        if phase == 'paused' and crew.get('resume_at') and now_ms() >= crew['resume_at']:
            crew.update(phase=crew.get('paused_from') or 'planning', reason=None, resume_at=None, paused_from=None)
            self.say(crew, 'finance', 'Usage window reset — resuming.', kind='finance')
            return
        if phase not in WORKING:
            return
        wait = crew.get('wait')
        if wait:
            return self.check_reply(crew, wait)
        if phase == 'ideating':
            self.ask(crew, 'ideator', 'ideas', self.ideation_task(crew))
        elif phase == 'planning':
            self.ask(crew, 'planner', 'plan', self.planning_task(crew))
        elif phase == 'building':
            self.build_step(crew)

    def check_reply(self, crew, wait):
        s = server.MANAGER.owned((crew.get('roles') or {}).get(wait['role']))
        if not s:
            # The role's process went away (released, crashed, kovan restarted without the holder): bring it back.
            s = self.role_session(crew, wait['role'])
            if s.status != 'working' and not (s.last_result_at and s.last_result_at >= wait['sent_at']):
                s.send(wait.get('prompt') or 'Please continue.', [])
                wait['sent_at'] = time.time()
                self.save(crew)
            return
        if s.permissions:
            self.handle_permissions(crew, s, wait['role'])
            return
        if crew.get('attention'):
            crew['attention'] = None
            self.save(crew)
        if s.status in ('ready',) and s.last_result_at and s.last_result_at >= wait['sent_at']:
            crew['wait'] = None
            data = extract_json(s.last_result)
            if data is None:
                if wait.get('retries', 0) >= 1:
                    raise server.KovanError('%s did not return the JSON it was asked for' % TITLES[wait['role']])
                crew['wait'] = dict(wait, retries=wait.get('retries', 0) + 1, sent_at=time.time())
                s.send('Your reply did not end with the JSON object I asked for. Reply again and end with only '
                       'that JSON object in a ```json block.', [])
                self.save(crew)
                return
            getattr(self, 'on_' + wait['step'])(crew, data, s.last_result)
            return
        if time.time() - wait['sent_at'] > REPLY_TIMEOUT:
            raise server.KovanError('%s has been working for over %d hours' % (TITLES[wait['role']], REPLY_TIMEOUT // 3600))

    def handle_permissions(self, crew, s, role):
        for pid, item in list(s.permissions.items()):
            if item['tool'] in ('AskUserQuestion', 'ExitPlanMode'):
                pass
            elif auto_allow(crew['options']['policy'], role, item['tool'], item['input'], crew['options']['check'],
                            crew['dir']):
                s.answer_permission(pid, 'allow', None, '')
                continue
            attention = {'role': role, 'sid': s.sid, 'tool': item['tool'], 'id': pid,
                         'summary': attention_summary(item['tool'], item['input'])}
            if crew.get('attention') != attention:
                crew['attention'] = attention
                self.say(crew, TITLES[role].lower(), 'Needs your OK: %s' % attention['summary'], kind='attention')
            return

    # ---- prompts
    def ideation_task(self, crew):
        seen = [i.get('title') for i in crew.get('ideas') or []]
        return ('Propose 4 project ideas%s.\n\nConstraints and wishes:\n%s\n%s'
                'JSON shape: {"ideas": [{"title": str, "summary": "2-3 sentences", "why": "the niche and why it is '
                'underserved", "stack": str, "novelty_check": "what you searched and found", "scores": {"depth": int, '
                '"usefulness": int, "novelty": int, "decomposability": int, "buildability": int}, "total": int}]}' % (
                    (' about: ' + crew['theme']) if crew.get('theme') else '', crew.get('theme') or '(none)',
                    ('Do not repeat these ideas: ' + '; '.join(seen) + '\n\n') if seen else ''))

    def planning_task(self, crew):
        feedback = crew.get('plan_feedback')
        current = crew.get('plan')
        task = ('Goal:\n%s\n\nWorkspace (%s):\n%s\n\nCheck command configured by the operator: %s\n\n' % (
            crew['goal'], crew['dir'], workspace_listing(crew['dir']), crew['options']['check'] or '(none — propose one)'))
        if feedback and current:
            task += 'Your previous plan:\n%s\n\nThe operator asked for changes:\n%s\n\n' % (
                milestones_text(current.get('milestones') or []), feedback)
        return task + ('Plan the work. JSON shape: {"summary": "one paragraph", "stack": str, "check_command": '
                       '"the command that proves the work, e.g. python3 -m pytest -q", "milestones": [{"title": str, '
                       '"goal": "2-4 sentences", "acceptance": ["verifiable criterion", ...]}]}')

    def develop_task(self, crew, m, number, total):
        plan = crew['plan']
        done = [x['title'] for x in plan['milestones'] if x['status'] == 'done']
        task = ('Project: %s\nPlan summary: %s\n\nMilestone %d of %d: %s\n%s\nAcceptance criteria:\n%s\n\n'
                'Already done: %s\nCheck command: %s\n' % (
                    crew['goal'], plan.get('summary', ''), number, total, m['title'], m['goal'],
                    '\n'.join('- ' + a for a in m['acceptance']) or '- (see goal)', ', '.join(done) or 'nothing yet',
                    crew['options']['check'] or plan.get('check_command') or '(none)'))
        if crew.get('feedback'):
            task += '\nThis is attempt %d. Fix these problems first:\n%s\n' % (m['attempts'] + 1, crew['feedback'])
        return task + ('\nImplement the milestone. JSON shape: {"status": "done" | "blocked", "summary": "what you '
                       'changed and why", "files": [str], "blocker": "only if blocked: what you need from a human"}')

    def test_task(self, crew, m, dev_summary):
        return ('Milestone: %s\n%s\nAcceptance criteria:\n%s\n\nThe Developer says:\n%s\n\nCheck command: %s\n\n'
                'Add or strengthen tests for every criterion and run them. JSON shape: {"status": "pass" | "fail", '
                '"summary": str, "failures": "concrete reproduction if anything fails"}' % (
                    m['title'], m['goal'], '\n'.join('- ' + a for a in m['acceptance']), dev_summary,
                    crew['options']['check'] or crew['plan'].get('check_command') or '(none)'))

    def review_task(self, crew, m, diff, check, dev_summary):
        return ('Milestone: %s\n%s\nAcceptance criteria:\n%s\n\nThe Developer says:\n%s\n\nCheck command result:\n%s\n\n'
                'The staged diff for this milestone:\n```diff\n%s\n```\n\nReview it. JSON shape: {"verdict": "approve" | '
                '"changes", "summary": str, "issues": [{"severity": "high" | "medium" | "low", "file": str, "note": str}], '
                '"commit_message": "conventional commit subject, e.g. feat(parser): add table support"}' % (
                    m['title'], m['goal'], '\n'.join('- ' + a for a in m['acceptance']), dev_summary, check, diff))

    def replan_task(self, crew, m, outcome):
        remaining = [x for x in crew['plan']['milestones'] if x['status'] == 'pending']
        return ('Milestone “%s” just finished: %s\n\nRemaining milestones:\n%s\n\nDecide what happens next. Pick '
                '"continue" when the last run was clean and the next milestone is small; "amend" to tighten the '
                'remaining milestones; "split" when the next one is genuinely too big; "escalate" when a human must '
                'decide. JSON shape: {"action": "continue" | "amend" | "split" | "escalate", "reason": str, '
                '"milestones": [only for amend/split: the full new list of REMAINING milestones, same shape as in the '
                'plan], "question": "only for escalate"}' % (m['title'], outcome, milestones_text(remaining)))

    # ---- reply handlers
    def on_ideas(self, crew, data, raw):
        ideas = [i for i in (data.get('ideas') or []) if isinstance(i, dict) and i.get('title')][:6]
        if not ideas:
            raise server.KovanError('the Ideator returned no ideas')
        crew['ideas'] = (crew.get('ideas') or []) + ideas
        for i in ideas:
            self.say(crew, 'ideator', '**%s** (%s/100) — %s' % (i.get('title'), i.get('total', '?'), i.get('summary', '')),
                     kind='idea', save=False)
        crew.update(phase='choosing', reason='pick an idea')
        self.say(crew, 'kovan', 'Pick an idea to build, or ask for more.', kind='waiting')

    def on_plan(self, crew, data, raw):
        milestones = clean_milestones(data.get('milestones'))
        if not milestones:
            raise server.KovanError('the Planner returned no milestones')
        crew['plan'] = {'summary': str(data.get('summary') or '')[:3000], 'stack': str(data.get('stack') or '')[:500],
                        'check_command': str(data.get('check_command') or '')[:500], 'milestones': milestones}
        proposed = crew['plan']['check_command']
        crew['plan_feedback'] = None
        if not crew['options']['check'] and proposed and not crew['options']['plan_approval']:
            # Nobody reviews the plan, so kovan only runs a proposed check command that is on the safe list.
            if auto_allow('safe', 'developer', 'Bash', {'command': proposed}, root=crew['dir']):
                crew['options']['check'] = proposed
            else:
                self.say(crew, 'kovan', 'The Planner proposed `%s` as the check command. It is not on the safe list, so '
                         'kovan will not run it unreviewed; set a check command on the crew to enable the CI gate.' % proposed,
                         kind='status', save=False)
        self.say(crew, 'planner', '%s\n\n%s' % (crew['plan']['summary'], milestones_text(milestones)), kind='plan', to='you')
        if crew['options']['plan_approval']:
            crew.update(phase='plan_review', reason='approve the plan')
            check = crew['options']['check'] or proposed
            self.say(crew, 'kovan', 'Waiting for your approval of the plan. %s' % (
                ('Check command after every milestone: `%s`.' % check) if check else 'No check command: only the '
                'Reviewer gates each milestone.'), kind='waiting')
        else:
            crew.update(phase='building', step='develop', current=0)
            self.save(crew)

    def build_step(self, crew):
        plan = crew['plan']['milestones']
        i = crew['current']
        if i >= len(plan):          # the last commit already posted the finance report
            crew.update(phase='done', reason=None, step=None)
            self.say(crew, 'kovan', 'All milestones are done.', kind='done')
            return
        m = plan[i]
        step = crew.get('step') or 'develop'
        if step == 'develop':
            if m['status'] == 'pending':
                m['status'] = 'active'
                self.say(crew, 'kovan', 'Milestone %d/%d: **%s**' % (i + 1, len(plan), m['title']), kind='milestone',
                         to='developer', milestone=m['id'], save=False)
            self.ask(crew, 'developer', 'develop', self.develop_task(crew, m, i + 1, len(plan)))
        elif step == 'test':
            self.ask(crew, 'tester', 'test', self.test_task(crew, m, m.get('dev_summary', '')))
        elif step == 'check':
            self.run_background(crew, 'check', self.do_check, m)
        elif step == 'review':
            self.run_background(crew, 'diff', self.do_review, m)
        elif step == 'commit':
            self.run_background(crew, 'commit', self.do_commit, m)
        elif step == 'replan':
            outcome = 'committed %s after %d attempt(s). Developer: %s' % (
                m.get('commit') or '(no commit)', m['attempts'] + 1, server.clip(m.get('dev_summary', ''), 600))
            self.ask(crew, 'planner', 'replan', self.replan_task(crew, m, outcome))

    def run_background(self, crew, name, fn, m):
        self.busy[crew['id']] = name

        def work():
            try:
                fn(crew, m)
            except Exception as e:  # noqa: BLE001
                server.log('crew %s %s failed: %r' % (crew['id'], name, e))
                self.block(crew, '%s failed: %s' % (name, e))
            finally:
                self.busy.pop(crew['id'], None)
                server.BUS.publish({'type': 'crew', 'id': crew['id']})
        threading.Thread(target=work, daemon=True, name='crew-' + name).start()

    def fail_attempt(self, crew, m, who, feedback):
        m['attempts'] += 1
        m['log'].append({'ts': now_ms(), 'who': who, 'result': 'rejected', 'text': server.clip(feedback, 3000)})
        crew['feedback'] = feedback
        if m['attempts'] >= crew['options']['max_attempts']:
            m['status'] = 'failed'
            crew.update(phase='blocked', reason='“%s” failed %d times' % (m['title'], m['attempts']), step='develop')
            self.say(crew, 'kovan', 'Escalating: “%s” did not pass after %d attempts. Retry (optionally with a hint), '
                     'skip it, or stop.' % (m['title'], m['attempts']), kind='blocked', milestone=m['id'])
        else:
            crew['step'] = 'develop'
            self.say(crew, who, 'Back to the Developer (attempt %d/%d).' % (m['attempts'] + 1, crew['options']['max_attempts']),
                     kind='handoff', to='developer', milestone=m['id'])

    def on_develop(self, crew, data, raw):
        m = crew['plan']['milestones'][crew['current']]
        summary = str(data.get('summary') or '').strip()
        m['dev_summary'] = server.clip(summary, 3000)
        if str(data.get('status')).lower() == 'blocked':
            m['log'].append({'ts': now_ms(), 'who': 'developer', 'result': 'blocked', 'text': data.get('blocker') or summary})
            crew.update(phase='blocked', reason='Developer needs you: %s' % server.clip(str(data.get('blocker') or summary), 300))
            self.say(crew, 'developer', 'I am blocked: %s' % (data.get('blocker') or summary), kind='blocked', to='you',
                     milestone=m['id'])
            return
        files = [str(f) for f in (data.get('files') or [])][:20]
        m['log'].append({'ts': now_ms(), 'who': 'developer', 'result': 'done', 'text': m['dev_summary'], 'files': files})
        crew['feedback'] = None
        nxt = 'test' if crew['options']['tester'] else 'check'
        crew['step'] = nxt
        self.say(crew, 'developer', '%s%s' % (summary or 'Done.', ('\n\nFiles: ' + ', '.join(files)) if files else ''),
                 kind='handoff', to='tester' if nxt == 'test' else 'reviewer', milestone=m['id'])

    def on_test(self, crew, data, raw):
        m = crew['plan']['milestones'][crew['current']]
        if str(data.get('status')).lower() != 'pass':
            self.say(crew, 'tester', 'Tests fail: %s' % (data.get('failures') or data.get('summary')), kind='verdict',
                     to='developer', milestone=m['id'], save=False)
            self.fail_attempt(crew, m, 'tester', 'The Tester reports: %s' % (data.get('failures') or data.get('summary')))
            return
        m['log'].append({'ts': now_ms(), 'who': 'tester', 'result': 'pass', 'text': str(data.get('summary') or '')})
        crew['step'] = 'check'
        self.say(crew, 'tester', 'Tests pass. %s' % (data.get('summary') or ''), kind='handoff', to='reviewer', milestone=m['id'])

    def do_check(self, crew, m):
        command = crew['options']['check']
        if not command:
            with self.lock:
                m['check'] = None
                crew['step'] = 'review'
                self.save(crew)
            return
        started = time.time()
        try:
            r = subprocess.run(command, shell=True, cwd=crew['dir'], capture_output=True, text=True,  # noqa: S602
                               timeout=crew['options']['check_timeout'], env=check_env())
            code, output = r.returncode, (r.stdout or '') + (r.stderr or '')
        except subprocess.TimeoutExpired:
            code, output = 124, 'timed out after %ds' % crew['options']['check_timeout']
        output, _ = server.mask_secrets(server.head_tail(output, 4000, 4000))
        with self.lock:
            crew['stats']['checks'] = crew['stats'].get('checks', 0) + 1
            m['check'] = {'code': code, 'output': output, 'seconds': round(time.time() - started, 1)}
            if code != 0:
                self.say(crew, 'kovan', 'Check failed (`%s`, exit %d):\n```\n%s\n```' % (command, code, output[-1800:]),
                         kind='check', to='developer', milestone=m['id'], save=False)
                self.fail_attempt(crew, m, 'kovan', 'The check command `%s` failed with exit code %d:\n%s' % (
                    command, code, output[-3000:]))
            else:
                crew['step'] = 'review'
                self.say(crew, 'kovan', 'Check passed (`%s`, %.0fs).' % (command, m['check']['seconds']), kind='check',
                         to='reviewer', milestone=m['id'])

    def do_review(self, crew, m):
        held = []
        if is_git_repo(crew['dir']):
            held = stage(crew['dir'])
            stat = git(crew['dir'], 'diff', '--cached', '--stat')[1]
            diff = git(crew['dir'], 'diff', '--cached', timeout=120)[1]
        else:
            stat, diff = '', '(not a git repository — review the workspace files directly)'
        diff, _ = server.mask_secrets(server.head_tail(diff, 30000, 6000))
        check = m.get('check')
        check_text = 'not configured' if not check else ('passed' if check['code'] == 0 else 'failed:\n' + check['output'][-1500:])
        with self.lock:
            m['diffstat'] = server.clip(stat, 2000)
            if held and m.get('held_back') != held[:50]:
                m['held_back'] = held[:50]
                self.say(crew, 'kovan', 'Kept out of the commit (they look like secrets): %s' % ', '.join(held[:10]),
                         kind='status', milestone=m['id'], save=False)
            self.ask(crew, 'reviewer', 'review', self.review_task(crew, m, diff or '(empty diff)', check_text,
                                                                   m.get('dev_summary', '')))
            if not crew.get('wait'):
                crew['step'] = 'review'    # role busy: try again on the next tick

    def on_review(self, crew, data, raw):
        m = crew['plan']['milestones'][crew['current']]
        issues = [i for i in (data.get('issues') or []) if isinstance(i, dict)]
        listing = '\n'.join('- [%s] %s%s' % (i.get('severity', '?'), (i.get('file') + ': ') if i.get('file') else '',
                                             i.get('note', '')) for i in issues)
        if str(data.get('verdict')).lower() != 'approve':
            self.say(crew, 'reviewer', 'Changes requested. %s\n%s' % (data.get('summary') or '', listing), kind='verdict',
                     to='developer', milestone=m['id'], save=False)
            self.fail_attempt(crew, m, 'reviewer', 'The Reviewer requested changes:\n%s\n%s' % (data.get('summary') or '', listing))
            return
        m['commit_message'] = re.sub(r'\s+', ' ', str(data.get('commit_message') or '')).strip()[:120] or \
            'feat: %s' % m['title']
        m['log'].append({'ts': now_ms(), 'who': 'reviewer', 'result': 'approved', 'text': str(data.get('summary') or '')})
        crew['step'] = 'commit'
        self.say(crew, 'reviewer', 'Approved. %s%s' % (data.get('summary') or '', ('\n' + listing) if listing else ''),
                 kind='verdict', to='kovan', milestone=m['id'])

    def do_commit(self, crew, m):
        sha, note = None, ''
        if crew['options']['commit'] and is_git_repo(crew['dir']):
            stage(crew['dir'])
            body = 'Milestone: %s\n\n%s' % (m['title'], server.clip(m.get('dev_summary', ''), 1500))
            code, out, err = git(crew['dir'], 'commit', '-q', '-m', m.get('commit_message') or m['title'], '-m', body)
            if code == 0:
                sha = git(crew['dir'], 'rev-parse', '--short', 'HEAD')[1].strip()
                if crew['options']['push'] and git(crew['dir'], 'remote', 'get-url', 'origin')[0] == 0:
                    pc, _, perr = git(crew['dir'], 'push', '-q', 'origin', 'HEAD', timeout=180)
                    note = ' and pushed' if pc == 0 else ' (push failed: %s)' % server.clip(perr, 200)
            elif 'nothing to commit' in (out + err):
                note = ' (nothing to commit)'
            else:
                note = ' (commit failed: %s)' % server.clip(err or out, 300)
        with self.lock:
            m.update(status='done', commit=sha)
            self.say(crew, 'kovan', '“%s” is done%s%s.' % (m['title'], ' — committed %s' % sha if sha else '', note),
                     kind='milestone', milestone=m['id'], save=False)
            self.finance_report(crew)
            more = any(x['status'] == 'pending' for x in crew['plan']['milestones'])
            crew['step'] = 'replan' if more else None
            if not more:
                crew['current'] = len(crew['plan']['milestones'])
            self.save(crew)

    def on_replan(self, crew, data, raw):
        action = str(data.get('action') or 'continue').lower()
        reason = str(data.get('reason') or '')
        plan = crew['plan']['milestones']
        if action in ('amend', 'split'):
            new = clean_milestones(data.get('milestones'))
            if new:
                kept = [x for x in plan if x['status'] != 'pending']
                crew['plan']['milestones'] = kept + new
        self.say(crew, 'planner', '%s — %s' % (action.capitalize(), reason or 'no reason given'), kind='replan', to='you')
        if action == 'escalate':
            crew.update(phase='blocked', reason='Planner asks: %s' % server.clip(str(data.get('question') or reason), 300))
            self.say(crew, 'planner', str(data.get('question') or reason), kind='blocked', to='you')
            return
        self.advance_milestone(crew)

    def advance_milestone(self, crew):
        crew['current'] = self.next_pending(crew)
        crew['step'] = 'develop'
        crew['feedback'] = None
        if crew['current'] >= len(crew['plan']['milestones']):
            crew.update(phase='done', reason=None, step=None)
            self.say(crew, 'kovan', 'All milestones are done.', kind='done')
        elif crew['options']['milestone_approval']:
            crew.update(phase='milestone_review', reason='continue to the next milestone')
            self.say(crew, 'kovan', 'Milestone finished — waiting for you before the next one.', kind='waiting')
        else:
            crew['phase'] = 'building'
            self.save(crew)


class Runner(threading.Thread):
    def __init__(self, crews):
        super().__init__(daemon=True, name='crew-runner')
        self.crews = crews

    def run(self):
        while True:
            time.sleep(1.0)
            try:
                self.crews.tick()
            except Exception as e:  # noqa: BLE001
                server.log('crew runner error: %r' % e)
