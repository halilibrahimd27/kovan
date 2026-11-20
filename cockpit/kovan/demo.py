"""Synthetic sessions for ``kovan --demo``: a small team working on a fictional shop.

Everything here is made up. The transcripts are written in the same on-disk format Claude Code
uses, into a throwaway directory, so the demo exercises the real reader, store and UI code. Two
of the sessions are "driven by kovan" and simulated in-process (one streams text, one waits for
an answer); write actions are disabled in demo mode.
"""
import json
import os
import random
import tempfile
import threading
import time
import uuid

from . import server

NS = uuid.UUID('6f1d1c0a-6b9d-4c8e-9f3a-2d7f0c5e8a11')


def sid_for(name):
    return str(uuid.uuid5(NS, name))


def iso(ms):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(ms / 1000)) + '.%03dZ' % (ms % 1000)


class Writer:
    """Builds a transcript in Claude Code's JSONL format."""

    def __init__(self, sid, start_ms, title=None, model='claude-opus-5-5', effort='xhigh'):
        self.sid, self.t, self.model, self.effort = sid, start_ms, model, effort
        self.lines, self.parent, self.context = [], None, 24000
        if title:
            self.lines.append({'type': 'custom-title', 'customTitle': title, 'sessionId': sid})

    def _tick(self, seconds):
        self.t += int(seconds * 1000)
        return self.t

    def _base(self, kind, seconds):
        u = str(uuid.uuid4())
        rec = {'parentUuid': self.parent, 'isSidechain': False, 'type': kind}
        self.parent = u
        return rec, u, iso(self._tick(seconds))

    def user(self, text, after=30):
        rec, u, ts = self._base('user', after)
        rec.update({'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]}, 'uuid': u, 'timestamp': ts,
                    'origin': {'kind': 'human'}, 'promptSource': 'sdk', 'sessionId': self.sid})
        self.lines.append(rec)
        return u

    def _assistant(self, block, after):
        rec, u, ts = self._base('assistant', after)
        self.context += random.randint(1500, 6000)
        rec.update({'message': {'model': self.model, 'role': 'assistant', 'content': [block],
                                'usage': {'input_tokens': 6, 'cache_read_input_tokens': self.context, 'cache_creation_input_tokens': 900,
                                          'output_tokens': 400}},
                    'effort': self.effort, 'uuid': u, 'timestamp': ts, 'sessionId': self.sid})
        self.lines.append(rec)

    def say(self, text, after=20):
        self._assistant({'type': 'text', 'text': text}, after)

    def tool(self, name, inp, output='', result=None, error=False, after=15, result_after=6):
        tid = 'toolu_' + uuid.uuid4().hex[:24]
        self._assistant({'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}, after)
        rec, u, ts = self._base('user', result_after)
        rec.update({'message': {'role': 'user', 'content': [{'tool_use_id': tid, 'type': 'tool_result', 'content': output,
                                                             'is_error': error}]},
                    'toolUseResult': result if result is not None else {}, 'uuid': u, 'timestamp': ts, 'sessionId': self.sid})
        self.lines.append(rec)
        return tid

    def bash(self, description, command, stdout, after=15, background_id=None):
        result = {'stdout': stdout, 'stderr': '', 'interrupted': False, 'isImage': False}
        inp = {'command': command, 'description': description}
        if background_id:
            inp['run_in_background'] = True
            result['backgroundTaskId'] = background_id
            stdout = 'Command running in background with ID: %s' % background_id
            result['stdout'] = stdout
        return self.tool('Bash', inp, stdout, result, after=after)

    def edit(self, path, hunks, after=15):
        old = '\n'.join(l[1:] for h in hunks for l in h['lines'] if l[0] in ' -')
        new = '\n'.join(l[1:] for h in hunks for l in h['lines'] if l[0] in ' +')
        return self.tool('Edit', {'file_path': path, 'old_string': old, 'new_string': new},
                         'The file %s has been updated successfully.' % path,
                         {'filePath': path, 'oldString': old, 'newString': new, 'structuredPatch': hunks}, after=after)

    def write(self, path, content, after=15):
        return self.tool('Write', {'file_path': path, 'content': content}, 'File created successfully at: %s' % path,
                         {'type': 'create', 'filePath': path, 'content': content, 'structuredPatch': []}, after=after)

    def send(self, to, summary, message, after=10):
        return self.tool('SendMessage', {'to': to, 'summary': summary, 'message': message},
                         'Message delivered to %s.' % to, {}, after=after)

    def receive(self, sender, body, after=60):
        rec, u, ts = self._base('attachment', after)
        prompt = '<cross-session-message from="uds:/tmp/cc-socks/0.sock" from-name="%s">\n%s\n</cross-session-message>' % (sender, body)
        rec.update({'attachment': {'type': 'queued_command', 'prompt': prompt,
                                   'origin': {'kind': 'peer', 'name': sender, 'body': body, 'msg_id': str(uuid.uuid4())}},
                    'uuid': u, 'timestamp': ts, 'sessionId': self.sid})
        self.lines.append(rec)

    def save(self, folder):
        with open(os.path.join(folder, self.sid + '.jsonl'), 'w', encoding='utf-8') as f:
            for rec in self.lines:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + '\n')


def hunk(old_start, new_start, lines):
    return {'oldStart': old_start, 'oldLines': sum(1 for l in lines if l[0] in ' -'),
            'newStart': new_start, 'newLines': sum(1 for l in lines if l[0] in ' +'), 'lines': lines}


STREAM_TEXT = (
    "The planner does a sequential scan on `orders` (≈1.2M rows) for every customer lookup, which is where the "
    "380 ms goes. I'll add a migration:\n\n"
    "```sql\nCREATE INDEX CONCURRENTLY idx_orders_customer_id\n    ON orders (customer_id);\n```\n\n"
    "`CONCURRENTLY` keeps writes flowing during the deploy. With the index the same query plans as an index scan "
    "and runs in **4 ms** on a copy of production data.\n\n"
    "Next steps:\n- add the migration file and a rollback\n- re-run the order and refund test suites\n"
    "- tell Infra the migration is ready for the 18:10 window"
)


def build():
    random.seed(7)
    root = tempfile.mkdtemp(prefix='kovan-demo-')
    claude_home = os.path.join(root, 'claude')
    project = os.path.join(os.path.expanduser('~'), 'code', 'acme-shop')
    folder = server.project_folder(claude_home, project)
    os.makedirs(folder)
    os.makedirs(os.path.join(claude_home, 'sessions'))
    now = int(time.time() * 1000)
    m = 60 * 1000
    ids = {n: sid_for(n) for n in ('api', 'release', 'infra', 'security', 'docs', 'perf', 'deps', 'onboarding')}

    # -- API refactor (driven by kovan, streaming)
    w = Writer(ids['api'], now - 52 * m, 'API refactor')
    w.user('Move the /orders endpoints onto the new service layer. Keep the public API identical and make sure the tests pass.')
    w.say("I'll start by reading the current handlers and the service layer, then move one endpoint at a time so the "
          "tests stay green between steps.")
    w.tool('Read', {'file_path': project + '/src/api/orders.py'}, '...', {})
    w.edit(project + '/src/services/orders.py', [hunk(12, 12, [
        ' class OrderService:',
        '     def __init__(self, db):',
        '         self.db = db',
        '+',
        '+    def create(self, customer_id, items):',
        '+        total = sum(i.price * i.quantity for i in items)',
        '+        return self.db.insert("orders", customer_id=customer_id, total=total, items=items)',
        '+',
        '+    def search(self, customer_id, status=None):',
        '+        query = "SELECT * FROM orders WHERE customer_id = %s"',
        '+        params = [customer_id]',
        '+        if status:',
        '+            query += " AND status = %s"',
        '+            params.append(status)',
        '+        return self.db.fetch(query, params)'])])
    w.edit(project + '/src/api/orders.py', [hunk(84, 84, [
        ' @router.get("/orders/search")',
        ' def search_orders(customer_id: int, status: str = None):',
        '-    sql = f"SELECT * FROM orders WHERE customer_id = {customer_id}"',
        '-    if status:',
        '-        sql += f" AND status = \'{status}\'"',
        '-    return db.fetch(sql)',
        '+    return services.orders.search(customer_id, status)'])])
    w.bash('Run the order test suite', 'pytest tests/orders -q',
           '........................                                   [100%]\n24 passed in 3.41s')
    w.say('All **24** order tests pass with the endpoints on the service layer. Next I\'ll migrate the refund flow.')
    w.receive('Security review', "Heads-up: /orders/search builds its WHERE clause with string formatting "
              "(src/api/orders.py:88). Please make sure the refactor uses bound parameters — I'm filing it as HIGH.", after=240)
    w.say("Good catch — that handler is exactly the one I just replaced. I'll confirm with Security.", after=20)
    w.send('Security review', 'Search now uses bound parameters',
           'Thanks — the new OrderService.search() only uses bound parameters and the old f-string handler is removed '
           'in this change. I will ping you when it is merged.')
    w.receive('Infra', "FYI: I'm restarting staging-replica-2 at 18:00 UTC (about 5 minutes). If your migration dry-run is scheduled "
              "for then, please run it against the primary or wait until 18:10.", after=400)
    w.say('Noted — I will run the migration dry-run against the primary.', after=15)
    w.user('Great. Also add an index for orders.customer_id if the slow customer page needs one.', after=200)
    w.bash('Explain the slow customer query', 'psql $DATABASE_URL -c "EXPLAIN ANALYZE SELECT * FROM orders WHERE customer_id = 4211"',
           'Seq Scan on orders  (cost=0.00..48211.40 rows=37 width=212) (actual time=0.031..381.902 rows=41 loops=1)\n'
           '  Filter: (customer_id = 4211)\n  Rows Removed by Filter: 1198733\nPlanning Time: 0.112 ms\nExecution Time: 381.950 ms')
    w.save(folder)
    api_context = w.context

    # -- Release prep (driven by kovan, waiting for an answer)
    w = Writer(ids['release'], now - 34 * m, 'Release prep')
    w.user('Prepare the 2.4 release: changelog, version bump, and a draft announcement.')
    w.bash('List merged pull requests since v2.3.1', 'gh pr list --state merged --search "merged:>2026-11-01" --json number,title',
           '#412 Move orders endpoints to the service layer\n#409 Add refund webhooks\n#405 Faster customer page\n'
           '#401 Upgrade payment SDK to 7.x\n#398 Fix timezone in invoice PDFs')
    w.edit(project + '/CHANGELOG.md', [hunk(1, 1, [
        ' # Changelog',
        ' ',
        '+## 2.4.0 — unreleased',
        '+',
        '+### Added',
        '+- Refund webhooks (#409)',
        '+',
        '+### Changed',
        '+- Orders endpoints now run on the service layer (#412)',
        '+- Customer page loads ~90x faster thanks to a new index (#405)',
        '+- Payment SDK upgraded to 7.x (#401)',
        '+',
        '+### Fixed',
        '+- Invoice PDFs use the customer\'s timezone (#398)',
        '+',
        ' ## 2.3.1 — 2026-10-28'])])
    w.say('The changelog is drafted from the five merged pull requests. One decision before I bump versions and write '
          'the announcement:')
    question = {'questions': [
        {'question': 'Which version should this release be tagged as?', 'header': 'Version', 'multiSelect': False,
         'options': [{'label': 'v2.4.0', 'description': 'New refund webhooks → minor release (recommended)'},
                     {'label': 'v2.3.2', 'description': 'Treat it as a patch and hold back the webhooks'}]},
        {'question': 'Where should the announcement go?', 'header': 'Channels', 'multiSelect': True,
         'options': [{'label': 'Blog', 'description': 'Long-form post with screenshots'},
                     {'label': 'Newsletter', 'description': 'Short summary for customers'},
                     {'label': 'Changelog page', 'description': 'Only the public changelog'}]}]}
    w.tool('AskUserQuestion', question, '', {}, after=8)
    w.save(folder)

    # -- Infra (VS Code, working)
    w = Writer(ids['infra'], now - 41 * m, 'Infra')
    w.user('Staging has been slow since this morning. Find out why and fix it if it is safe.')
    w.bash('Check replication lag on the staging replicas', 'psql -h staging-replica-2 -c "SELECT now() - pg_last_xact_replay_timestamp();"',
           ' ?column?\n-----------------\n 00:14:07.218331\n(1 row)')
    w.bash('Tail the staging API logs', 'kubectl logs -f deploy/api -n staging --since=10m', '', background_id='b7x2k9m4q')
    w.say('Replica 2 is **14 minutes** behind: a long-running analytics query holds a snapshot and blocks replay. '
          'Reads that hit that replica return stale data, which is why the customer page looks slow and inconsistent.')
    w.send('API refactor', 'Staging DB maintenance at 18:00',
           "FYI: I'm restarting staging-replica-2 at 18:00 UTC (about 5 minutes). If your migration dry-run is scheduled "
           "for then, please run it against the primary or wait until 18:10.")
    w.tool('Bash', {'command': 'psql -h staging-replica-2 -c "SELECT pg_cancel_backend(48213)"',
                    'description': 'Cancel the runaway analytics query on replica 2'}, '', {}, after=30, result_after=1)
    w.lines.pop()   # still running: no result yet
    w.save(folder)

    # -- Security review (VS Code, your turn)
    w = Writer(ids['security'], now - 33 * m, 'Security review')
    w.user('Do a security pass over the orders and auth modules before the release.')
    w.tool('Grep', {'pattern': 'f"SELECT', 'path': 'src'}, 'src/api/orders.py:88', {})
    w.bash('Look for credentials committed to the repository', 'git grep -nE "(PASSWORD|API_KEY|SECRET)=" -- config/',
           'config/staging.env:3:DB_PASSWORD=hunter2-staging\n'
           'config/staging.env:4:STRIPE_API_KEY=sk_test_51Hdemo0000000000000000\n'
           'config/example.env:2:SESSION_SECRET=change-me')
    w.send('API refactor', 'SQL injection in /orders/search',
           "Heads-up: /orders/search builds its WHERE clause with string formatting (src/api/orders.py:88). Please make "
           "sure the refactor uses bound parameters — I'm filing it as HIGH.", after=40)
    w.edit(project + '/src/auth/session.py', [hunk(31, 31, [
        ' def set_session_cookie(response, token):',
        '     response.set_cookie(',
        '         "session", token,',
        '-        httponly=False,',
        '+        httponly=True,',
        '+        secure=True,',
        '+        samesite="Lax",',
        '         max_age=SESSION_TTL,',
        '     )'])], after=90)
    w.receive('API refactor', 'Thanks — the new OrderService.search() only uses bound parameters and the old f-string handler '
              'is removed in this change. I will ping you when it is merged.', after=200)
    w.say("Review complete.\n\n| Severity | Finding | Status |\n|---|---|---|\n"
          "| High | SQL injection in `/orders/search` | fixed in *API refactor* |\n"
          "| Medium | Staging credentials committed in `config/staging.env` | rotate + move to the secret store |\n"
          "| Medium | Session cookie without `HttpOnly`/`Secure` | fixed in `src/auth/session.py` |\n"
          "| Low | Example secret `change-me` in `config/example.env` | document only |\n\n"
          "The staging database password and the Stripe test key should be rotated today; I have not touched them.",
          after=30)
    w.save(folder)

    # -- Docs (terminal, idle)
    w = Writer(ids['docs'], now - 160 * m, 'Docs')
    w.user('Update the API reference for the orders endpoints.')
    w.write(project + '/docs/api/orders.md', '# Orders API\n\n## GET /orders/search\n\nQuery parameters:\n\n'
            '| name | type | required |\n|---|---|---|\n| customer_id | int | yes |\n| status | string | no |\n')
    w.say('The orders reference is updated and matches the current handlers.')
    w.save(folder)

    # -- closed sessions
    for key, title, ago, prompt, reply in (
            ('perf', 'Perf investigation', 2 * 24 * 60, 'Why is the customer page slow?', 'It is the missing index on orders.customer_id; details in the notes.'),
            ('deps', 'Dependency upgrade', 26 * 60, 'Upgrade the payment SDK to 7.x.', 'Upgraded; all payment tests pass.'),
            ('onboarding', 'Onboarding notes', 3 * 24 * 60, 'Write onboarding notes for new contributors.', 'Done — see docs/onboarding.md.')):
        w = Writer(ids[key], now - ago * m, title)
        w.user(prompt)
        w.say(reply, after=240)
        w.save(folder)

    # -- live sessions (VS Code / terminal) are recognised by ~/.claude/sessions/<pid>.json
    pid = os.getpid()
    for key, name, status, entry, started in (('infra', 'Infra', 'busy', 'claude-vscode', 41),
                                               ('security', 'Security review', 'idle', 'claude-vscode', 33),
                                               ('docs', 'Docs', 'idle', 'cli', 160)):
        rec = {'pid': pid, 'sessionId': ids[key], 'cwd': project, 'startedAt': now - started * m, 'kind': 'interactive',
               'entrypoint': entry, 'name': name, 'nameSource': 'user', 'status': status, 'updatedAt': now}
        with open(os.path.join(claude_home, 'sessions', '%s.json' % key), 'w') as f:
            json.dump(rec, f)

    # -- background task output for Infra
    tasks_root = os.path.join(root, 'tmp')
    task_dir = os.path.join(tasks_root, 'claude-%d' % os.getuid(), 'demo', ids['infra'], 'tasks')
    os.makedirs(task_dir)
    with open(os.path.join(task_dir, 'b7x2k9m4q.output'), 'w') as f:
        for i in range(40):
            f.write('2026-11-20T17:%02d:%02dZ GET /customers/%d 200 %dms replica=%s\n'
                    % (40 + i // 30, (i * 7) % 60, 4000 + i * 13, 240 + (i * 37) % 400, 'replica-2' if i % 3 else 'primary'))

    # -- notes
    notes_dir = os.path.join(root, 'notes')
    os.makedirs(notes_dir)
    journal = os.path.join(notes_dir, 'JOURNAL.md')
    with open(journal, 'w') as f:
        f.write('# Team journal\n\n'
                '## Mon — customer page investigation\n- Found the sequential scan on `orders`.\n- Decision: add an index in 2.4.\n\n'
                '## Tue — payment SDK 7.x\n- Upgraded, all payment tests green.\n\n'
                '## Wed — security pass\n- SQL injection in `/orders/search` (HIGH) → fixed by the service layer refactor.\n'
                '- Staging credentials committed → rotate today.\n\n'
                '## Thu — release prep\n- Changelog drafted for 2.4.0.\n- Staging replica restart at 18:00 UTC.\n')
    todo = os.path.join(notes_dir, 'TODO.md')
    with open(todo, 'w') as f:
        f.write('# Open items\n\n- [ ] Rotate the staging database password and Stripe test key\n'
                '- [ ] Merge the service layer refactor (#412)\n- [ ] Tag the release once the version is decided\n'
                '- [x] Fix the session cookie flags\n')

    crews = build_crews(claude_home, now, m)
    return {'root': root, 'claude_home': claude_home, 'dirs': [project] + crews['dirs'], 'notes': [journal, todo],
            'task_roots': [tasks_root], 'ids': ids, 'question': question, 'api_context': api_context,
            'crews': crews, 'swarm': build_swarm(now, m)}


class DemoSession(server.ManagedSession):
    """A kovan-driven session simulated in-process (no claude process)."""

    def write(self, obj):
        raise server.KovanError('Demo mode is read-only.', 403)

    def request(self, subtype, params=None, timeout=20):
        raise server.KovanError('Demo mode is read-only.', 403)

    def release(self):
        raise server.KovanError('Demo mode is read-only.', 403)


DEMO_MODELS = [{'value': 'default', 'name': 'Default (recommended)'}, {'value': 'opus', 'name': 'Opus 5.5'},
               {'value': 'sonnet', 'name': 'Sonnet 5.5'}, {'value': 'haiku', 'name': 'Haiku 5.5'}]
DEMO_COMMANDS = [{'name': 'compact', 'description': 'Summarize the conversation to free up context', 'hint': ''},
                 {'name': 'effort', 'description': 'Set how hard the model thinks', 'hint': 'low|medium|high|xhigh|max'},
                 {'name': 'model', 'description': 'Switch the model', 'hint': ''},
                 {'name': 'review', 'description': 'Review the current changes', 'hint': ''}]


def attach(manager, bus, info):
    now = time.time()
    server.SETTINGS.data['archived'] = [info['ids']['onboarding']]
    manager.limit = {'five_hour': {'percent': 42, 'resets_at': int(now + 2.4 * 3600), 'status': 'allowed'},
                     'seven_day': {'percent': 61, 'resets_at': int(now + 3 * 86400), 'status': 'allowed'},
                     'status': 'allowed', 'ts': int(now)}

    api = DemoSession(info['ids']['api'], 'API refactor', True, info['dirs'][0], pid=0)
    api.status, api.model, api.permission_mode = 'working', 'claude-opus-5-5', 'acceptEdits'
    api.models, api.commands, api.started_at = DEMO_MODELS, DEMO_COMMANDS, now - 3000
    api.context = {'total': info['api_context'], 'max': 1000000, 'percent': round(info['api_context'] / 10000), 'threshold': 967000}
    api.turn_started = now - 95

    rel = DemoSession(info['ids']['release'], 'Release prep', True, info['dirs'][0], pid=0)
    rel.status, rel.model, rel.permission_mode = 'ready', 'claude-opus-5-5', 'default'
    rel.models, rel.commands, rel.started_at = DEMO_MODELS, DEMO_COMMANDS, now - 2000
    rel.context = {'total': 61000, 'max': 1000000, 'percent': 6, 'threshold': 967000}
    rel.permissions['demo-question'] = {'id': 'demo-question', 'tool': 'AskUserQuestion', 'input': info['question'],
                                        'title': None, 'description': None, 'ts': int(now * 1000)}
    manager.sessions[api.sid] = api
    manager.sessions[rel.sid] = rel

    def stream():
        while True:
            api.tool = {'text': 'Running EXPLAIN ANALYZE with the new index', 'name': 'Bash'}
            api.publish('tool', tool=api.tool)
            time.sleep(3)
            api.tool, api.streaming = None, ''
            api.publish('text_start')
            i = 0
            while i < len(STREAM_TEXT):
                n = random.randint(2, 7)
                api.streaming += STREAM_TEXT[i:i + n]
                api.publish('chunk', text=STREAM_TEXT[i:i + n])
                i += n
                time.sleep(0.045)
            time.sleep(8)
            api.streaming = ''
            api.publish('text_start')
    threading.Thread(target=stream, daemon=True, name='demo-stream').start()


# ============================================================================ crews and the swarm (synthetic)
CREW_PLAN = [
    ('Parse tables out of Markdown', 'Find pipe tables in a Markdown document and return them with their line ranges; '
     'everything outside tables must round-trip byte for byte.',
     ['Tables inside lists and block quotes are found', 'Escaped pipes and pipes in inline code stay in their cell',
      'A document without tables is returned unchanged']),
    ('Align columns', 'Pad every cell to its column width, honouring :--, :-: and --: alignment.',
     ['Column width uses display width (CJK and emoji count as two)', 'Alignment markers are preserved']),
    ('Command-line interface', '`tablefmt FILE...` rewrites files in place; `--check` reports without writing; stdin '
     'when no file is given.', ['`--check` exits 1 when a file would change, with no output on stdout',
                                'Standard library only', 'Exit codes are documented']),
    ('Pre-commit hook', 'Ship a `.pre-commit-hooks.yaml` so the formatter runs on staged Markdown files.',
     ['The hook exits non-zero when it changes a file', 'Works on a fresh clone with `pre-commit try-repo`']),
    ('Package and document', 'pyproject metadata, a README with examples, and a release checklist.',
     ['`pip install .` provides the `tablefmt` command', 'README shows before/after examples']),
]


def crew_milestones(now, m):
    out = []
    for i, (title, goal, acc) in enumerate(CREW_PLAN):
        status = 'done' if i < 2 else 'active' if i == 2 else 'pending'
        ms = {'id': 'm%06x' % (0xa11ce0 + i), 'title': title, 'goal': goal, 'acceptance': acc, 'status': status,
              'attempts': 1 if i in (1, 2) else 0, 'commit': {0: '3f9c2a1', 1: '8b41d07'}.get(i), 'log': []}
        if i < 3:
            ms['check'] = {'code': 0, 'output': '%d passed' % (9 + 5 * i), 'seconds': 2.0 + i}
        if i == 2:
            ms['log'] = [{'ts': now - 6 * m, 'who': 'reviewer', 'result': 'rejected',
                          'text': '--check prints the diff to stdout; it must stay silent for hooks.'}]
        out.append(ms)
    return out


def crew_board(now, m):
    seq = [
        (95, 'you', None, 'goal', 'A command-line tool and Python library that reformats Markdown tables: aligns columns, '
         'normalises separators, handles CJK and emoji widths and leaves everything outside tables untouched. '
         'Ship it with tests and a pre-commit hook.'),
        (94.8, 'kovan', None, 'status', 'Crew created in ~/code/tablefmt. The Planner goes first.'),
        (94.7, 'kovan', None, 'status', 'Planner joined (a new Claude Code session).'),
        (91, 'planner', 'you', 'plan', 'A small pure-Python package with a thin CLI: parsing and formatting are pure '
         'functions so they are easy to test, and the CLI only does I/O.\n\n' + '\n'.join(
             '%d. %s — %s' % (i + 1, t, g) for i, (t, g, _) in enumerate(CREW_PLAN))),
        (90.9, 'kovan', None, 'waiting', 'Waiting for your approval of the plan. Check command after every milestone: '
         '`python3 -m pytest -q`.'),
        (88, 'you', None, 'decision', 'Plan approved — go ahead.'),
        (87.9, 'kovan', 'developer', 'milestone', 'Milestone 1/5: **Parse tables out of Markdown**'),
        (79, 'developer', 'tester', 'handoff', 'Added `tablefmt.parse.find_tables()`: a line scanner that tracks fenced code '
         'blocks, so pipes in code are never mistaken for tables.\n\nFiles: tablefmt/parse.py, tests/test_parse.py'),
        (74, 'tester', 'reviewer', 'handoff', 'Tests pass. Added 9 cases: escaped pipes, inline code with pipes, tables '
         'inside lists and quotes, and a byte-for-byte round trip of a document without tables.'),
        (73.5, 'kovan', 'reviewer', 'check', 'Check passed (`python3 -m pytest -q`, 2s).'),
        (70, 'reviewer', 'kovan', 'verdict', 'Approved. The scanner is small and every acceptance criterion has a test.\n'
         '- [low] parse.py: `_split_row` deserves a docstring'),
        (69.8, 'kovan', None, 'milestone', '“Parse tables out of Markdown” is done — committed 3f9c2a1.'),
        (69.7, 'finance', None, 'finance', '4 role turns so far; 5-hour usage 18%.'),
        (68, 'planner', 'you', 'replan', 'Continue — a clean run, and the next milestone is small.'),
        (67.9, 'kovan', 'developer', 'milestone', 'Milestone 2/5: **Align columns**'),
        (58, 'developer', 'tester', 'handoff', 'Column widths come from the widest cell; alignment from the separator row. '
         'Files: tablefmt/align.py, tests/test_align.py'),
        (54, 'tester', 'developer', 'verdict', 'Tests fail: a row with a CJK cell is one column short, because `len()` '
         'counts characters, not display width.\n```\n| 名前 | qty |\n|------|-----|   ← expected |------|\n```'),
        (53.9, 'tester', 'developer', 'handoff', 'Back to the Developer (attempt 2/3).'),
        (47, 'developer', 'tester', 'handoff', 'Display width now uses `unicodedata.east_asian_width`; emoji count as '
         'two. Added the failing case as a regression test.'),
        (44, 'tester', 'reviewer', 'handoff', 'Tests pass. 14 cases including CJK, emoji and mixed alignment.'),
        (43.6, 'kovan', 'reviewer', 'check', 'Check passed (`python3 -m pytest -q`, 3s).'),
        (41, 'reviewer', 'kovan', 'verdict', 'Approved. Width handling is isolated in one function with direct tests.'),
        (40.8, 'kovan', None, 'milestone', '“Align columns” is done — committed 8b41d07.'),
        (40.7, 'finance', None, 'finance', '9 role turns so far; 5-hour usage 31%.'),
        (39, 'you', 'developer', 'note', 'Keep the CLI dependency-free please — standard library only.'),
        (38, 'planner', 'you', 'replan', 'Amend — tightened milestone 4: the hook must exit non-zero when it changes a '
         'file, so commits are blocked until the table is formatted.'),
        (37.9, 'kovan', 'developer', 'milestone', 'Milestone 3/5: **Command-line interface**'),
        (24, 'developer', 'tester', 'handoff', '`tablefmt FILE...` rewrites in place, `--check` reports without writing, '
         'stdin is read when no file is given. Standard library only (argparse), as you asked.'),
        (19, 'tester', 'reviewer', 'handoff', 'Tests pass. The CLI is exercised through a subprocess, including the '
         '`--check` exit codes.'),
        (18.5, 'kovan', 'reviewer', 'check', 'Check passed (`python3 -m pytest -q`, 4s).'),
        (6, 'reviewer', 'developer', 'verdict', 'Changes requested. `--check` prints the diff to stdout, but the acceptance '
         'criteria say it must stay silent so it can run in hooks.\n- [medium] cli.py: write the diff to stderr, keep '
         'stdout empty\n- [low] README: document the exit codes'),
        (5.9, 'reviewer', 'developer', 'handoff', 'Back to the Developer (attempt 2/3).'),
    ]
    return [{'id': 'b%04d' % i, 'ts': int(now - ago * m), 'speaker': sp, 'to': to, 'kind': kind, 'text': text,
             'milestone': None} for i, (ago, sp, to, kind, text) in enumerate(seq)]


IDEAS = [
    {'title': 'driftwatch', 'summary': 'Compares the effective configuration of two environments (env vars, feature flags, '
     'service versions) and explains every difference in plain words before a release.',
     'why': 'Staging/production drift causes a large share of release-day surprises; existing tools diff files, not the '
     'effective config a service actually sees.', 'stack': 'Python, Typer, pluggable collectors',
     'novelty_check': 'Searched for config drift detectors: infrastructure-level tools exist (Terraform plan, driftctl), '
     'nothing at the application-config level.',
     'scores': {'depth': 26, 'usefulness': 22, 'novelty': 16, 'decomposability': 12, 'buildability': 8}, 'total': 84},
    {'title': 'flakehunter', 'summary': 'Re-runs failing tests in isolation and in random orders to classify them as flaky, '
     'order-dependent or real, then bisects the shared state that causes it.',
     'why': 'Teams quarantine flaky tests by hand; telling order dependence from real failures is tedious.',
     'stack': 'Python, pytest plugin', 'novelty_check': 'pytest-rerunfailures retries, pytest-randomly shuffles; '
     'neither bisects the culprit.',
     'scores': {'depth': 25, 'usefulness': 21, 'novelty': 15, 'decomposability': 12, 'buildability': 8}, 'total': 81},
    {'title': 'schemaguard', 'summary': 'Runs every migration against a throwaway Postgres, diffs the schema and flags '
     'operations that take long locks on big tables.', 'why': 'Lock-heavy migrations are the classic way to take a '
     'small team’s database down at noon.', 'stack': 'Python, Docker, SQL parsing',
     'novelty_check': 'Partly covered by squawk (lints SQL); none replays migrations against real Postgres.',
     'scores': {'depth': 24, 'usefulness': 21, 'novelty': 12, 'decomposability': 12, 'buildability': 8}, 'total': 77},
    {'title': 'commit-changelog', 'summary': 'Generates release notes from conventional commits.',
     'why': 'Saves a few minutes per release.', 'stack': 'Python',
     'novelty_check': 'Well covered by git-cliff, release-please and semantic-release — not novel.',
     'scores': {'depth': 12, 'usefulness': 16, 'novelty': 4, 'decomposability': 14, 'buildability': 10}, 'total': 56},
]


def build_crews(claude_home, now, m):
    """Two crews: one mid-build with a lively boardroom, one waiting for you to pick an idea."""
    home = os.path.expanduser('~')
    table_dir, idea_dir = os.path.join(home, 'code', 'tablefmt'), os.path.join(home, 'code', 'weekend-idea')
    roles = {r: sid_for('crew-tablefmt-' + r) for r in ('planner', 'developer', 'tester', 'reviewer')}
    ideator = sid_for('crew-weekend-ideator')
    folder = server.project_folder(claude_home, table_dir)
    os.makedirs(folder)
    for role, first, last, start in (
            ('planner', 'Plan the work. JSON shape: {"summary": …, "milestones": […]}', 'Amend — tightened milestone 4.', 92),
            ('developer', 'Milestone 3 of 5: Command-line interface', '`tablefmt FILE...` rewrites in place; `--check` '
             'reports without writing.', 88),
            ('tester', 'Add or strengthen tests for every criterion and run them.', 'Tests pass.', 80),
            ('reviewer', 'Review the staged diff for this milestone.', 'Changes requested: `--check` must keep stdout '
             'empty.', 71)):
        w = Writer(roles[role], now - start * m, 'tablefmt · ' + role.capitalize())
        w.user(first)
        if role == 'developer':
            w.tool('Read', {'file_path': table_dir + '/tablefmt/cli.py'}, '...', {})
            w.bash('Run the test suite', 'python3 -m pytest -q', '....................                    [100%]\n'
                   '19 passed in 3.87s')
        w.say(last, after=200)
        if role == 'developer':
            w.user('Your milestone came back from the Reviewer. Fix these problems first: `--check` prints the diff to '
                   'stdout …', after=600)
            w.edit(table_dir + '/tablefmt/cli.py', [hunk(41, 41, [
                '     if args.check and changed:',
                '-        print(diff)',
                '+        sys.stderr.write(diff)',
                '         return 1'])])
        w.save(folder)
    folder = server.project_folder(claude_home, idea_dir)
    os.makedirs(folder)
    w = Writer(ideator, now - 30 * m, 'Weekend idea · Ideator')
    w.user('Propose 4 project ideas about: developer tools for small teams; command line; Python; no paid services.')
    w.tool('WebSearch', {'query': 'config drift detection application level open source'}, '...', {})
    w.say('Here are four ideas, scored honestly — the last one is not novel.', after=240)
    w.save(folder)
    opts = dict(tester=True, plan_approval=True, milestone_approval=False, policy='safe', max_attempts=3,
                quota_pause=90, check='python3 -m pytest -q', check_timeout=900, commit=True, push=False)
    table = {'id': 'c7ab1e5f0', 'name': 'tablefmt', 'mode': 'goal', 'dir': table_dir, 'options': opts,
             'goal': crew_board(now, m)[0]['text'], 'theme': '', 'created_at': int(now - 95 * m), 'updated_at': int(now - 5.9 * m),
             'phase': 'building', 'reason': None, 'roles': roles, 'primed': {r: True for r in roles},
             'plan': {'summary': 'A small pure-Python package with a thin CLI: parsing and formatting are pure functions so '
                                 'they are easy to test, and the CLI only does I/O.', 'stack': 'Python 3.9+, standard library, pytest',
                      'check_command': 'python3 -m pytest -q', 'milestones': crew_milestones(now, m)},
             'ideas': [], 'current': 2, 'step': 'develop', 'wait': {'role': 'developer', 'step': 'develop', 'sent_at': (now - 5.8 * m) / 1000},
             'feedback': None, 'plan_feedback': None, 'notes': [], 'board': crew_board(now, m), 'attention': None,
             'resume_at': None, 'paused_from': None, 'stats': {'turns': 14, 'checks': 3}}
    idea_board = [
        {'id': 'i0', 'ts': int(now - 31 * m), 'speaker': 'you', 'to': None, 'kind': 'goal', 'milestone': None,
         'text': 'Suggest projects: developer tools for small teams; command line; Python; no paid services.'},
        {'id': 'i1', 'ts': int(now - 30.9 * m), 'speaker': 'kovan', 'to': None, 'kind': 'status', 'milestone': None,
         'text': 'Crew created in ~/code/weekend-idea. The Ideator goes first.'},
    ] + [{'id': 'i%d' % (i + 2), 'ts': int(now - (26 - i) * m), 'speaker': 'ideator', 'to': 'you', 'kind': 'idea',
          'milestone': None, 'text': '**%s** (%d/100) — %s' % (x['title'], x['total'], x['summary'])}
         for i, x in enumerate(IDEAS)] + [
        {'id': 'i9', 'ts': int(now - 21.5 * m), 'speaker': 'kovan', 'to': None, 'kind': 'waiting', 'milestone': None,
         'text': 'Pick an idea to build, or ask for more.'}]
    weekend = {'id': 'c0dea1dea', 'name': 'Weekend idea', 'mode': 'ideas', 'dir': idea_dir,
               'options': dict(opts, tester=False, check=''), 'goal': '',
               'theme': 'developer tools for small teams; command line; Python; no paid services',
               'created_at': int(now - 31 * m), 'updated_at': int(now - 21.5 * m), 'phase': 'choosing',
               'reason': 'pick an idea', 'roles': {'ideator': ideator}, 'primed': {'ideator': True}, 'plan': None,
               'ideas': IDEAS, 'current': 0, 'step': None, 'wait': None, 'feedback': None, 'plan_feedback': None,
               'notes': [], 'board': idea_board, 'attention': None, 'resume_at': None, 'paused_from': None,
               'stats': {'turns': 1, 'checks': 0}}
    return {'dirs': [table_dir, idea_dir], 'items': [table, weekend], 'roles': roles, 'ideator': ideator}


def build_swarm(now, m):
    ledger, pager = str(uuid.uuid5(NS, 'swarm-ledgerline')), str(uuid.uuid5(NS, 'swarm-quietpager'))
    projects = [
        {'id': ledger, 'name': 'ledgerline', 'state': 'building', 'detail': 'M4 reconciliation engine — attempt 1',
         'repo': 'example/ledgerline', 'done': 3, 'total': 6, 'cost': 4.82},
        {'id': pager, 'name': 'quietpager', 'state': 'awaiting_approval', 'detail': 'plan ready — waiting for your approval',
         'repo': '', 'done': 0, 'total': 5, 'cost': 0.61}]

    def e(i, ago, role, text, kind='decision'):
        return {'id': 'e%03d' % i, 'ts': int(now - ago * m), 'speaker': role, 'kind': kind, 'text': text}
    ledger_board = [
        e(1, 300, 'PM', 'Scoped v1: import bank CSV and OFX, categorise with rules, reconcile against invoices. '
          'Multi-currency is out of scope.'),
        e(2, 296, 'Architect', 'Stack: Python 3.12, SQLite, Typer. Reconciliation is a pure function over two sorted '
          'streams, so it is property-testable.'),
        e(3, 280, 'Operator', 'Please keep OFX parsing behind an interface — we may add CAMT.053 later.'),
        e(4, 276, 'PM', 'Amended M2: an `Importer` protocol with CSV and OFX implementations; CAMT.053 noted for v2.'),
        e(5, 230, 'developer', 'Implementing the CSV importer with a dialect sniffer and explicit date formats.', 'message'),
        e(6, 214, 'Reviewer', 'M2 approved: importer protocol plus CSV/OFX, 31 tests, no network access.'),
        e(7, 190, 'Finance', 'Spend so far $3.10 of the $20 project budget; daily cap at 22%.'),
        e(8, 150, 'tester', 'OFX fixtures with CRLF line endings fail on the second run.', 'message'),
        e(9, 140, 'Diagnostician', 'M3 failed twice on CRLF line endings in OFX: the parser assumed \\n. Normalise line '
          'endings at the importer boundary.'),
        e(10, 120, 'Reviewer', 'M3 approved after the fix; CRLF fixtures added to the suite.'),
        e(11, 118, 'PM', 'Replan: continue with M4 (reconciliation engine); M5 and M6 unchanged.'),
        e(12, 60, 'developer', 'Implementing `reconcile()` as a merge of two sorted streams…', 'message'),
        e(13, 35, 'tester', 'Hypothesis found a counterexample: two payments of the same amount on the same day match '
          'the same invoice twice.', 'message'),
        e(14, 12, 'Finance', '$4.82 spent; projected total $9–11 for the project.'),
    ]
    pager_board = [
        e(21, 50, 'PM', 'The Critic accepted the idea (86/100): an on-call pager that batches low-severity alerts into '
          'digests and only wakes someone for what is actionable.'),
        e(22, 44, 'Architect', 'Stack: Go, one binary, SQLite; alert sources via webhooks; no external services.'),
        e(23, 40, 'PM', 'Plan ready: 5 milestones. Waiting for operator approval before building.'),
    ]
    return {'projects': projects, 'boards': {ledger: ledger_board, pager: pager_board}}


def attach_crews(crews, info):
    now = time.time()
    data = info['crews']
    for c in data['items']:
        crews.items[c['id']] = c
    crews.reindex()
    crews.revision += 1
    dev = DemoSession(data['roles']['developer'], 'tablefmt · Developer', True, data['dirs'][0], pid=0)
    dev.status, dev.model, dev.permission_mode, dev.started_at = 'working', 'claude-opus-5-5', 'acceptEdits', now - 5000
    dev.models, dev.commands, dev.turn_started = DEMO_MODELS, DEMO_COMMANDS, now - 340
    dev.tool = {'text': 'Edit tablefmt/cli.py', 'name': 'Edit'}
    server.MANAGER.sessions[dev.sid] = dev
    for role in ('planner', 'tester', 'reviewer'):
        s = DemoSession(data['roles'][role], 'tablefmt · ' + role.capitalize(), True, data['dirs'][0], pid=0)
        s.status, s.model, s.permission_mode, s.started_at = 'ready', 'claude-opus-5-5', 'acceptEdits', now - 5000
        s.models, s.commands = DEMO_MODELS, DEMO_COMMANDS
        server.MANAGER.sessions[s.sid] = s
    s = DemoSession(data['ideator'], 'Weekend idea · Ideator', True, data['dirs'][1], pid=0)
    s.status, s.model, s.permission_mode, s.started_at = 'ready', 'claude-opus-5-5', 'acceptEdits', now - 1900
    s.models, s.commands = DEMO_MODELS, DEMO_COMMANDS
    server.MANAGER.sessions[s.sid] = s
