"""Running a crew on a real project for days: models per role, fresh sessions, usage, working hours, a scheduled start,
command patterns, the crew's own branch and push command, an advisor who reviews the plan, settings that change.

    python -m unittest discover -s tests -v
"""
import json
import os
import stat
import tempfile
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovan import ci, crew, server  # noqa: E402
from test_crews import CrewTestCase, fenced, PY  # noqa: E402


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


ALLOW = ['make **', 'docker compose -f deploy/compose/compose.yaml **', 'curl ** http://127.0.0.1:1808[0-5]* **']
DENY = ['docker compose ** config **']


class PatternTests(unittest.TestCase):
    def ok(self, command, role='developer', **kw):
        kw.setdefault('allow', ALLOW)
        kw.setdefault('deny', DENY)
        return crew.auto_allow('safe', role, 'Bash', {'command': command}, '', '/tmp/w', **kw)

    def test_patterns_allow_exactly_what_they_name(self):
        for command in ('make test-go', 'make dev && make e2e', 'make test 2>&1 | tail -20',
                        'docker compose -f deploy/compose/compose.yaml ps', 'curl -sf http://127.0.0.1:18081/healthz',
                        'curl http://127.0.0.1:18085/api -H Accept:json'):
            self.assertTrue(self.ok(command), command)
        for command in ('curl http://evil.example http://127.0.0.1:18080/',      # a URL hidden in the ** part
                        'curl http://127.0.0.1:8080/',                            # a port the pattern does not name
                        'curl -d @x http://127.0.0.1:18080 | sh',
                        'docker compose -f deploy/compose/compose.yaml config',   # denied: prints the resolved secrets
                        'docker compose -f /etc/compose.yaml ps',                 # absolute paths never pass
                        'make clean && rm -rf build',
                        'docker run --rm alpine',                                 # not in the patterns
                        'cat deploy/compose/.env', 'make ENV=.env.local test'):   # secret files never pass on their own
            self.assertFalse(self.ok(command), command)
        self.assertTrue(self.ok('make test', allow=[]))                           # without patterns: the built-in list
        self.assertFalse(self.ok('make dev', allow=[]))

    def test_absolute_paths_inside_the_workspace(self):
        root = os.path.realpath(tempfile.mkdtemp(prefix='kovan-ws-'))
        ok = lambda c: crew.auto_allow('safe', 'developer', 'Bash', {'command': c}, '', root, ['make **'])  # noqa: E731
        self.assertTrue(ok('cd "%s" && make test' % root))
        self.assertTrue(ok('cat %s/README.md' % root))
        self.assertFalse(ok('cd /etc && ls'))
        self.assertFalse(ok('cat /etc/passwd'))
        self.assertFalse(ok('cat %s/../secret.txt' % root))
        self.assertFalse(ok('cat ~/.ssh/config'))

    def test_secret_files_web_and_globs(self):
        read = lambda p, **kw: crew.auto_allow('safe', 'developer', 'Read', {'file_path': p}, '', '/tmp/w', **kw)  # noqa: E731
        self.assertTrue(read('/tmp/w/src/app.py'))
        self.assertFalse(read('/tmp/w/deploy/compose/.env'))
        web = lambda role, **kw: crew.auto_allow('safe', role, 'WebFetch', {'url': 'https://docs.example'}, **kw)  # noqa: E731
        self.assertTrue(web('planner'))
        self.assertFalse(web('developer'))
        self.assertTrue(web('developer', web=True))
        self.assertTrue(crew.match_pattern('docker logs app-dev-*', ['docker', 'logs', 'app-dev-api']))
        self.assertFalse(crew.match_pattern('docker logs app-dev-*', ['docker', 'logs', 'other-app']))
        self.assertTrue(crew.match_pattern('make **', ['make']))

    def test_options_are_validated(self):
        base = crew.DEFAULT_OPTIONS
        good = crew.clean_options({'models': {'developer': 'sonnet', 'planner': 'claude-opus-5-5[1m]', 'tester': ''},
                                   'efforts': {'planner': 'xhigh'}, 'hours': {'days': [0, 1, 2, 3, 4], 'start': '8:00',
                                                                              'end': '17:00'},
                                   'allow': 'make **\n# a comment\n\n', 'branch': 'feature/m1-sso',
                                   'commit_trailer': '  Co-Authored-By: A <a@example.com>  '}, base)
        self.assertEqual(good['models'], {'developer': 'sonnet', 'planner': 'claude-opus-5-5[1m]'})
        self.assertEqual(good['hours'], {'days': [0, 1, 2, 3, 4], 'start': '08:00', 'end': '17:00'})
        self.assertEqual(good['allow'], ['make **'])
        self.assertEqual(good['commit_trailer'], 'Co-Authored-By: A <a@example.com>')
        for bad in ({'models': {'developer': 'opus; rm -rf'}}, {'efforts': {'planner': 'ultra'}},
                    {'hours': {'days': [1], 'start': '17:00', 'end': '08:00'}}, {'allow': ['make test | sh']},
                    {'branch': 'feature/../x'}, {'branch': '-x'}, {'advisor': {'sid': 'nope'}}):
            with self.assertRaises(server.KovanError, msg=bad):
                crew.clean_options(bad, base)

    def test_working_hours(self):
        hours = {'days': [0, 1, 2, 3, 4], 'start': '08:00', 'end': '17:00'}
        at = lambda *a: time.mktime((2026, 11) + a + (0, 0, 0, -1))  # noqa: E731
        self.assertTrue(crew.in_hours(hours, at(25, 15, 0)))                       # Wednesday 15:00
        self.assertFalse(crew.in_hours(hours, at(25, 17, 0)))                      # the end is exclusive
        self.assertEqual(crew.next_open(hours, at(25, 17, 30)), at(26, 8, 0))      # → Thursday 08:00
        self.assertEqual(crew.next_open(hours, at(27, 18, 0)), at(30, 8, 0))       # Friday evening → Monday
        self.assertEqual(crew.next_open(hours, at(26, 7, 0)), at(26, 8, 0))        # early morning → the same day
        self.assertTrue(crew.in_hours(None))
        self.assertEqual(crew.short_subject('feat: ' + 'word ' * 20), 'feat: ' + ' '.join(['word'] * 13))
        self.assertLessEqual(len(crew.short_subject('x' * 100)), 72)


class ProjectCrewTests(CrewTestCase):
    def build(self, c, n, usage=None):
        """Drive milestone n (1-based) through the Developer and the Reviewer."""
        self.drive(c, self.waiting_on(c, 'developer'))
        dev = self.role(c, 'developer')
        with open(os.path.join(self.work, 'step%d.txt' % n), 'w') as f:
            f.write('step %d\n' % n)
        dev.last_usage = usage
        dev.reply(fenced({'status': 'done', 'summary': 'Wrote step %d.' % n}))
        self.drive(c, self.waiting_on(c, 'reviewer'))
        self.role(c, 'reviewer').reply(fenced({'verdict': 'approve', 'summary': 'ok',
                                               'commit_message': 'feat: step %d with a subject that keeps going on and on '
                                                                 'well beyond seventy-two characters' % n}))
        return dev

    def test_models_fresh_sessions_and_usage(self):
        server.MANAGER.limit = {'seven_day': {'percent': 41}, 'five_hour': {'percent': 12}}
        c = self.new(models={'developer': 'sonnet', 'reviewer': 'opus'}, efforts={'developer': 'low'})
        self.drive(c, self.waiting_on(c, 'planner'))
        planner = self.role(c, 'planner')
        planner.last_usage = {'in': 10, 'out': 400, 'cache_read': 9000, 'cache_write': 2000, 'ms': 5000, 'context': 11000}
        planner.reply(fenced(self.plan(2)))
        usage = {'in': 5, 'out': 1000, 'cache_read': 50000, 'cache_write': 3000, 'ms': 60000, 'context': 53000}
        first = self.build(c, 1, usage)
        self.drive(c, self.waiting_on(c, 'planner'))
        self.assertTrue(first.released)                                           # milestone 1's Developer has left
        self.assertEqual(self.crews.role_of(first.sid)['role'], 'developer')     # … and still belongs to the crew
        self.assertTrue(self.crews.role_of(first.sid)['past'])
        server.MANAGER.limit = {'seven_day': {'percent': 44}, 'five_hour': {'percent': 20}}
        planner.reply(fenced({'action': 'continue', 'reason': 'clean'}))
        second = self.build(c, 2, usage)
        self.assertNotEqual(first.sid, second.sid)
        self.assertIn('You are the Developer', second.sent[0])                    # a fresh session gets the charter again
        self.assertIn('- Step 1 (commit ', second.sent[0])                        # … and what is already done
        self.assertIs(self.role(c, 'planner'), planner)                           # the Planner keeps its session
        self.drive(c, lambda c: c['phase'] == 'done')
        launches = {name.split(' · ')[-1]: launch for (_, name, _), launch in zip(server.MANAGER.started,
                                                                                  server.MANAGER.launches) if name}
        self.assertEqual((launches['Developer']['model'], launches['Developer']['effort']), ('sonnet', 'low'))
        self.assertEqual((launches['Planner']['model'], launches['Reviewer']['model']), (None, 'opus'))
        self.assertIn('Read(**/.env)', launches['Planner']['extra_args'][0])      # every role: no secret files
        u = self.crews.summary(c)['usage']
        self.assertEqual(u['roles']['developer']['turns'], 2)
        self.assertEqual(u['roles']['developer']['out'], 2000)
        self.assertEqual(u['planning']['out'], 400)
        self.assertEqual((u['week_start'], u['context']['developer']), (41, 53000))
        m1, m2 = c['plan']['milestones']
        # milestone 1: the Developer's turn and the Planner's replan after it
        self.assertEqual((m1['usage']['out'], m1['usage']['week_from'], m2['usage']['week_to']), (1400, 41, 44))
        subjects = self.git_log()[:2]
        self.assertTrue(all(len(s) <= 72 and s.startswith('feat: step') for s in subjects), subjects)
        self.assertIn('New milestone, fresh sessions: Developer, Reviewer.', [b['text'] for b in c['board']])

    def test_without_fresh_sessions_the_developer_stays(self):
        c = self.new(fresh=False)
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(2)))
        first = self.build(c, 1)
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced({'action': 'continue', 'reason': 'clean'}))
        self.assertIs(self.build(c, 2), first)

    def test_scheduled_start_working_hours_and_idle_command(self):
        marker = os.path.join(self.tmp, 'idle.txt')
        idle = '%s -c "open(%r, \'a\').write(\'down\\n\')"' % (PY, marker)
        c = self.new(start_at=int((time.time() + 3600) * 1000), hours={'days': list(range(7)), 'start': '00:00',
                                                                         'end': '00:01'}, idle_command=idle)
        self.assertEqual((c['phase'], c['pause_kind'], c['reason']), ('paused', 'scheduled', 'scheduled start'))
        self.assertTrue(any(b['text'].startswith('The crew starts at ') for b in c['board']))
        self.crews.tick()
        self.assertEqual(c['phase'], 'paused')                                    # not yet
        self.crews.update_options(c['id'], {'start_at': 0})                       # start now instead
        self.drive(c, lambda c: c['phase'] == 'paused' and c.get('pause_kind') == 'hours')   # … but it is after hours
        self.assertIn('Start time — the crew begins.', [b['text'] for b in c['board']])
        self.assertGreater(c['resume_at'], time.time() * 1000)
        self.drive(c, lambda c: os.path.exists(marker) and any(b['text'].startswith('Ran `') for b in c['board']))
        self.assertNotIn('planner', c['roles'])                                   # no turn started after hours
        self.crews.update_options(c['id'], {'hours': None})
        self.drive(c, self.waiting_on(c, 'planner'))
        self.assertIn('Working hours — resuming.', [b['text'] for b in c['board']])
        with self.assertRaisesRegex(server.KovanError, 'cannot change'):
            self.crews.update_options(c['id'], {'branch': 'x'})
        with self.assertRaisesRegex(server.KovanError, 'already started'):
            self.crews.update_options(c['id'], {'start_at': int(time.time() * 1000) + 99999})

    def test_a_reading_from_a_window_that_has_reset_never_pauses(self):
        # 27 Nov 2026: yesterday's "5-hour window at 93%, resets at 17:40" made a crew pause and resume every second
        server.MANAGER.limit = {'five_hour': {'percent': 93, 'resets_at': time.time() - 3600},
                                'seven_day': {'percent': 8, 'resets_at': time.time() + 86400}}
        c = self.new()
        self.drive(c, self.waiting_on(c, 'planner'))
        self.assertFalse(any(b['speaker'] == 'finance' for b in c['board']))
        self.assertIsNone(server.fresh_limit(server.MANAGER.limit)['five_hour']['percent'])

    def test_a_pause_without_a_reset_time_waits_and_never_flaps(self):
        server.MANAGER.limit = {'five_hour': {'percent': 95}}
        c = self.new()
        self.drive(c, lambda c: c['phase'] == 'paused')
        self.assertGreater(c['resume_at'], (time.time() + 500) * 1000)             # looks again in ten minutes
        for _ in range(30):
            self.crews.tick()
        self.assertEqual(c['phase'], 'paused')
        self.assertEqual(sum(b['speaker'] == 'finance' for b in c['board']), 1)
        c['resume_at'] = 1                                                         # time is up: the same reading again
        for _ in range(5):
            self.crews.tick()
        finance = [b['text'] for b in c['board'] if b['speaker'] == 'finance']
        self.assertEqual(c['phase'], 'paused')
        self.assertLessEqual(len(finance), 3, finance)                             # no flood, whatever happens

    def test_weekly_limit_pauses(self):
        server.MANAGER.limit = {'seven_day': {'percent': 96, 'resets_at': time.time() + 7200}, 'five_hour': {'percent': 3}}
        c = self.new(lang='tr')
        self.drive(c, lambda c: c['phase'] == 'paused')
        self.assertEqual((c['reason'], c['pause_kind']), ('usage limit', 'quota'))
        self.assertTrue(any(b['text'].startswith('Duraklıyorum: haftalık kullanım penceresi %96 (sınır %95).')
                            for b in c['board']), [b['text'] for b in c['board']])
        self.crews.update_options(c['id'], {'weekly_pause': 100})
        self.drive(c, self.waiting_on(c, 'planner'))

    def test_settings_change_while_running(self):
        c = self.new(lang='tr')
        self.drive(c, self.waiting_on(c, 'planner'))
        d = self.crews.update_options(c['id'], {'models': {'developer': 'sonnet'}, 'allow': ['make **'], 'check': ''})
        self.assertEqual(d['options']['models'], {'developer': 'sonnet'})
        self.assertIn('Ayarlar değişti: modeller, izinli komutlar, denetim komutu.', [b['text'] for b in c['board']])
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.drive(c, self.waiting_on(c, 'developer'))
        self.assertIn('`make **`', self.role(c, 'developer').sent[-1])            # the Developer knows what runs freely
        self.assertEqual(server.MANAGER.launches[-1]['model'], 'sonnet')


class BranchAndPushTests(CrewTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(self.work)
        sh(self.work, 'git', 'init', '-q', '-b', 'main')
        with open(os.path.join(self.work, 'README.md'), 'w') as f:
            f.write('# app\n')
        sh(self.work, 'git', 'add', '-A')
        sh(self.work, 'git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', 'commit', '-qm', 'init')
        sh(self.work, 'git', 'remote', 'add', 'origin', 'https://git.example.edu/team/app.git')
        self.log = os.path.join(self.tmp, 'pushed.txt')
        self.push = '%s -c "import sys; open(%r, \'a\').write(sys.argv[1] + \'\\n\')" {branch}' % (PY, self.log)

    def test_own_branch_push_command_trailer_and_request_link(self):
        c = self.new(pr=True, branch='feature/m1-sso', push_command=self.push,
                     commit_trailer='Co-Authored-By: Claude <noreply@example.com>')
        self.assertEqual(c['git'], {'forge': 'gitlab', 'base': 'main', 'branch': 'feature/m1-sso', 'pr_url': None})
        self.assertEqual(sh(self.work, 'git', 'branch', '--show-current').strip(), 'feature/m1-sso')
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.drive(c, self.waiting_on(c, 'developer'))
        with open(os.path.join(self.work, 'a.py'), 'w') as f:
            f.write('A = 1\n')
        self.role(c, 'developer').reply(fenced({'status': 'done', 'summary': 'a'}))
        self.drive(c, self.waiting_on(c, 'reviewer'))
        self.role(c, 'reviewer').reply(fenced({'verdict': 'approve', 'summary': 'ok', 'commit_message': 'feat: add a',
                                               'commit_body': 'Add the constant A.'}))
        self.drive(c, lambda c: c['phase'] == 'done')
        with open(self.log) as f:
            self.assertEqual(f.read().split(), ['feature/m1-sso', 'feature/m1-sso'])   # after the commit, and at the end
        body = sh(self.work, 'git', 'log', '-1', '--format=%B').strip()
        self.assertTrue(body.startswith('feat: add a\n\nAdd the constant A.\n'), body)   # the Reviewer's English body
        self.assertTrue(body.endswith('Co-Authored-By: Claude <noreply@example.com>'))
        link = ('https://git.example.edu/team/app/-/merge_requests/new?merge_request%5Bsource_branch%5D=feature%2Fm1-sso'
                '&merge_request%5Btarget_branch%5D=main')
        self.assertEqual(c['git']['request_url'], link)
        self.assertIn('Pushed feature/m1-sso. Open the merge request when you are ready: ' + link,
                      [b['text'] for b in c['board']])

    def test_a_scheduled_crew_branches_when_it_starts(self):
        c = self.new(pr=True, branch='feature/m1-sso', push_command=self.push, start_at=int((time.time() + 3600) * 1000))
        self.assertEqual(sh(self.work, 'git', 'branch', '--show-current').strip(), 'main')     # nothing changes yet
        self.assertTrue(c['git_at_start'])
        with open(os.path.join(self.work, 'wip.txt'), 'w') as f:                                 # someone works meanwhile
            f.write('x')
        self.crews.act(c['id'], 'resume')                                                        # start now
        self.drive(c, lambda c: c['phase'] == 'blocked')
        self.assertIn('uncommitted changes', c['reason'])
        os.remove(os.path.join(self.work, 'wip.txt'))
        self.crews.act(c['id'], 'retry')
        self.drive(c, self.waiting_on(c, 'planner'))
        self.assertEqual(sh(self.work, 'git', 'branch', '--show-current').strip(), 'feature/m1-sso')
        self.assertIn('Working on the branch feature/m1-sso (from main).', [b['text'] for b in c['board']])

    def build_one(self, c, name, message):
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.drive(c, self.waiting_on(c, 'developer'))
        with open(os.path.join(self.work, name), 'w') as f:
            f.write('X = 1\n')
        self.role(c, 'developer').reply(fenced({'status': 'done', 'summary': name}))
        self.drive(c, self.waiting_on(c, 'reviewer'))
        self.role(c, 'reviewer').reply(fenced({'verdict': 'approve', 'summary': 'ok', 'commit_message': message}))
        self.drive(c, lambda c: c['phase'] == 'done')

    def test_a_finished_crew_takes_the_next_goal_on_a_new_branch(self):
        c = self.new(pr=True, branch='feature/m1', push_command=self.push)
        with self.assertRaisesRegex(server.KovanError, 'not possible'):
            self.crews.act(c['id'], 'next', 'Too early.', branch='feature/m2')        # only a finished crew
        self.build_one(c, 'a.py', 'feat: add a')
        with self.assertRaisesRegex(server.KovanError, 'describe the next goal'):
            self.crews.act(c['id'], 'next', '  ', branch='feature/m2')
        with self.assertRaisesRegex(server.KovanError, 'new branch name'):
            self.crews.act(c['id'], 'next', 'Build the second thing.')
        with self.assertRaisesRegex(server.KovanError, 'must differ'):
            self.crews.act(c['id'], 'next', 'Build the second thing.', branch='feature/m1')     # still on it
        self.assertEqual((c['phase'], c['goal'], c.get('earlier')), ('done', 'Build a tiny thing.', None))   # nothing changed
        sh(self.work, 'git', 'checkout', '-q', 'main')                              # you merged it and updated the base
        sh(self.work, 'git', 'merge', '-q', '--ff-only', 'feature/m1')
        planner = self.role(c, 'planner')
        d = self.crews.act(c['id'], 'next', 'Build the second thing.', branch='feature/m2', name='Whole project')
        self.assertEqual((d['name'], d['phase'], d['goal']), ('Whole project', 'planning', 'Build the second thing.'))
        self.assertEqual([(x['goal'], x['branch']) for x in d['earlier']], [('Build a tiny thing.', 'feature/m1')])
        self.assertEqual(c['earlier'][0]['milestones'], [{'title': 'Step 1', 'status': 'done'}])
        self.assertEqual(sh(self.work, 'git', 'branch', '--show-current').strip(), 'main')     # the branch comes at the start
        self.build_one(c, 'b.py', 'feat: add b')
        self.assertIs(self.role(c, 'planner'), planner)                             # same crew, same roles
        self.assertIn('Build the second thing.', planner.sent[-1])
        self.assertIn('already finished 1 earlier goal(s)', planner.sent[-1])
        self.assertEqual(c['git']['branch'], 'feature/m2')
        self.assertEqual(c['git']['base'], 'main')
        self.assertEqual(sh(self.work, 'git', 'branch', '--show-current').strip(), 'feature/m2')
        self.assertEqual(sh(self.work, 'git', 'log', '--format=%s', '-2').split('\n')[:2], ['feat: add b', 'feat: add a'])
        with open(self.log) as f:
            self.assertEqual(f.read().split()[-1], 'feature/m2')
        self.assertIn('Working on the branch feature/m2 (from main).', [b['text'] for b in c['board']])

    def test_branch_rules_and_links(self):
        sh(self.work, 'git', 'branch', 'feature/taken')
        with self.assertRaisesRegex(server.KovanError, 'already exists'):
            self.new(branch='feature/taken')
        with self.assertRaisesRegex(server.KovanError, 'differ'):
            self.new(branch='main')
        self.assertEqual(ci.request_link(self.work, {'branch': 'kovan/x', 'base': 'main'}),
                         'https://git.example.edu/team/app/-/merge_requests/new?merge_request%5Bsource_branch%5D=kovan%2Fx'
                         '&merge_request%5Btarget_branch%5D=main')
        sh(self.work, 'git', 'remote', 'set-url', 'origin', 'git@github.com:example/app.git')
        self.assertEqual(ci.request_link(self.work, {'branch': 'b', 'base': 'main'}),
                         'https://github.com/example/app/compare/main...b?expand=1')

    def test_a_failing_push_command_is_reported(self):
        ok, err = ci.push(self.work, {'branch': 'b'}, '%s -c "import sys; print(\'hook: subject too long\'); sys.exit(1)"' % PY)
        self.assertEqual((ok, err), (False, 'hook: subject too long'))


FAKE_CLAUDE = r'''#!%s
import json, os, sys
with open(os.environ['FAKE_ADVISOR_LOG'], 'w') as f:
    json.dump({'args': sys.argv[1:], 'stdin': sys.stdin.read(), 'cwd': os.getcwd(),
               'env': sorted(k for k in os.environ if k.startswith(('CLAUDE_CODE_SESSION', 'KOVAN')))}, f)
print(json.dumps({'result': 'K1 should come before K2; otherwise fine.', 'session_id': 'aaaaaaaa-0000-4000-8000-000000000001'}))
'''


class AdvisorTests(CrewTestCase):
    def setUp(self):
        super().setUp()
        self.sid = str(uuid.uuid4())
        self.advisor_dir = os.path.join(self.tmp, 'advisor-home')
        os.makedirs(self.advisor_dir)
        server.STORE.transcripts = {self.sid: mock.Mock(directory=self.advisor_dir)}
        self.fake = os.path.join(self.tmp, 'claude')
        with open(self.fake, 'w') as f:
            f.write(FAKE_CLAUDE % PY)
        os.chmod(self.fake, os.stat(self.fake).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.tmp, 'advisor.json')
        os.environ['FAKE_ADVISOR_LOG'] = self.log
        self.patch = mock.patch.object(server, 'find_claude', return_value=self.fake)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.environ.pop('FAKE_ADVISOR_LOG', None)
        super().tearDown()

    def test_the_advisor_reviews_the_plan_once_before_you_approve(self):
        c = self.new(plan_approval=True, lang='tr',
                     advisor={'sid': self.sid, 'name': 'ZT', 'model': 'claude-opus-5-5[1m]', 'effort': 'xhigh'})
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(2)))
        self.drive(c, lambda c: c['phase'] == 'plan_review' and c['reason'] == 'approve the plan')
        board = c['board']
        advice = [b for b in board if b['speaker'] == 'advisor']
        self.assertEqual([b['text'] for b in advice], ['K1 should come before K2; otherwise fine.'])
        texts = [b['text'] for b in board]
        self.assertIn('Planı onaylamadan önce ZT gözden geçiriyor.', texts)
        self.assertLess(texts.index('K1 should come before K2; otherwise fine.'),
                        max(i for i, t in enumerate(texts) if t.startswith('Planın onayını bekliyorum.')))
        with open(self.log) as f:
            call = json.load(f)
        for arg in ('--resume=' + self.sid, '--fork-session', '--tools=Read,Grep,Glob', '--model=claude-opus-5-5[1m]',
                    '--effort=xhigh'):
            self.assertIn(arg, call['args'])
        self.assertEqual(os.path.realpath(call['cwd']), os.path.realpath(self.advisor_dir))   # where its transcript lives
        self.assertIn('Step 2', call['stdin'])
        self.assertIn('in Turkish', call['stdin'])
        self.assertEqual(self.crews.role_of('aaaaaaaa-0000-4000-8000-000000000001')['role'], 'advisor')
        os.remove(self.log)
        self.crews.act(c['id'], 'revise', 'smaller')                              # a revised plan goes straight to you
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.drive(c, lambda c: c['phase'] == 'plan_review' and c['reason'] == 'approve the plan')
        self.assertFalse(os.path.exists(self.log))

    def test_a_fresh_advisor_reads_the_project_with_your_brief_and_files(self):
        note = os.path.join(self.tmp, 'project-note.md')
        with open(note, 'w') as f:
            f.write('Decisions: the realm is code. password=hunter2\n')
        c = self.new(plan_approval=True, advisor={'sid': '', 'name': 'Reviewer of plans', 'model': 'opus', 'effort': 'xhigh',
                                                  'brief': 'Does the plan cover K0-K5?', 'files': [note]})
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.drive(c, lambda c: c['phase'] == 'plan_review' and c['reason'] == 'approve the plan')
        with open(self.log) as f:
            call = json.load(f)
        self.assertNotIn('--fork-session', call['args'])
        self.assertFalse(any(a.startswith('--resume') for a in call['args']))
        self.assertEqual(os.path.realpath(call['cwd']), os.path.realpath(self.work))       # reads the project itself
        self.assertIn('Does the plan cover K0-K5?', call['stdin'])
        self.assertIn('Decisions: the realm is code.', call['stdin'])
        self.assertNotIn('hunter2', call['stdin'])                                          # masked
        self.assertIn('K1 should come before K2; otherwise fine.', [b['text'] for b in c['board']])
        with self.assertRaisesRegex(server.KovanError, 'advisor file not found'):
            crew.clean_options({'advisor': {'files': ['/nonexistent/x.md']}}, crew.DEFAULT_OPTIONS)

    def test_an_unreachable_advisor_does_not_hold_up_the_plan(self):
        server.STORE.transcripts = {}
        c = self.new(plan_approval=True, advisor={'sid': self.sid, 'name': 'ZT'})
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.drive(c, lambda c: c['phase'] == 'plan_review' and c['reason'] == 'approve the plan')
        self.assertTrue(any(b['text'].startswith('The advisor could not review the plan: ') for b in c['board']))
        self.crews.act(c['id'], 'approve')
        self.drive(c, self.waiting_on(c, 'developer'))


if __name__ == '__main__':
    unittest.main()
