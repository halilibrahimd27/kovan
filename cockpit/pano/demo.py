"""Synthetic sessions for ``pano --demo``: a small team working on a fictional shop.

Everything here is made up. The transcripts are written in the same on-disk format Claude Code
uses, into a throwaway directory, so the demo exercises the real reader, store and UI code. Two
of the sessions are "driven by pano" and simulated in-process (one streams text, one waits for
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
    root = tempfile.mkdtemp(prefix='pano-demo-')
    claude_home = os.path.join(root, 'claude')
    project = os.path.join(os.path.expanduser('~'), 'code', 'acme-shop')
    folder = server.project_folder(claude_home, project)
    os.makedirs(folder)
    os.makedirs(os.path.join(claude_home, 'sessions'))
    now = int(time.time() * 1000)
    m = 60 * 1000
    ids = {n: sid_for(n) for n in ('api', 'release', 'infra', 'security', 'docs', 'perf', 'deps', 'onboarding')}

    # -- API refactor (driven by pano, streaming)
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

    # -- Release prep (driven by pano, waiting for an answer)
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

    return {'root': root, 'claude_home': claude_home, 'dirs': [project], 'notes': [journal, todo],
            'task_roots': [tasks_root], 'ids': ids, 'question': question, 'api_context': api_context}


class DemoSession(server.ManagedSession):
    """A pano-driven session simulated in-process (no claude process)."""

    def write(self, obj):
        raise server.PanoError('Demo mode is read-only.', 403)

    def request(self, subtype, params=None, timeout=20):
        raise server.PanoError('Demo mode is read-only.', 403)

    def release(self):
        raise server.PanoError('Demo mode is read-only.', 403)


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
