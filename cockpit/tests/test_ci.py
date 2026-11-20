"""Pull requests and the CI gate: real git with a local bare remote, a fake `gh` on PATH, fake role sessions.

    python -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovan import ci, crew, server  # noqa: E402
from test_crews import CrewTestCase, fenced, PY  # noqa: E402

FAKE_GH = r'''#!%s
"""A stand-in for the GitHub CLI: answers from a JSON state file and records every call."""
import json, os, re, sys
path = os.environ['FAKE_GH_STATE']
state = json.load(open(path))
args = sys.argv[1:]
state.setdefault('calls', []).append(args)
def save():
    json.dump(state, open(path, 'w'))
out = ''
if args[:1] == ['api']:
    url = args[1]
    if '/check-runs?' in url:
        seq = state.get('runs', [])
        runs = seq.pop(0) if len(seq) > 1 else (seq[0] if seq else [])
        out = json.dumps({'check_runs': runs})
    elif url.endswith('/status'):
        out = json.dumps({'state': 'pending', 'statuses': state.get('statuses', [])})
    elif url.endswith('/annotations'):
        out = json.dumps([{'path': 'test_app.py', 'start_line': 3, 'message': 'AssertionError: 2 != 3'}])
elif args[:3] == ['run', 'view', '--job']:
    out = 'FAILED test_app.py::test_add - assert 2 == 3\nDB_PASSWORD=hunter2\n'
elif args[:2] == ['pr', 'create']:
    out = 'https://github.com/example/app/pull/7\n'
save()
sys.stdout.write(out)
'''


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


class CiTestCase(CrewTestCase):
    def setUp(self):
        super().setUp()
        self.saved_ci = (crew.CI_POLL, crew.CI_FIRST_SEEN, os.environ.get('PATH'), os.environ.get('FAKE_GH_STATE'))
        crew.CI_POLL, crew.CI_FIRST_SEEN = 0.05, 1
        self.bare = os.path.join(self.tmp, 'remote.git')
        sh(self.tmp, 'git', 'init', '-q', '--bare', self.bare)
        os.makedirs(self.work)
        sh(self.work, 'git', 'init', '-q', '-b', 'main')
        with open(os.path.join(self.work, 'README.md'), 'w') as f:
            f.write('# app\n')
        sh(self.work, 'git', 'add', '-A')
        sh(self.work, 'git', 'commit', '-qm', 'init')
        bin_dir = os.path.join(self.tmp, 'bin')
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, 'gh'), 'w') as f:
            f.write(FAKE_GH % PY)
        os.chmod(os.path.join(bin_dir, 'gh'), 0o755)
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')
        os.environ['FAKE_GH_STATE'] = self.state_file = os.path.join(self.tmp, 'gh.json')
        self.gh({})

    def tearDown(self):
        crew.CI_POLL, crew.CI_FIRST_SEEN, path, state = self.saved_ci
        os.environ['PATH'] = path
        if state is None:
            os.environ.pop('FAKE_GH_STATE', None)
        super().tearDown()

    def gh(self, state=None):
        if state is not None:
            with open(self.state_file, 'w') as f:
                json.dump(state, f)
        with open(self.state_file) as f:
            return json.load(f)

    def github_remote(self):
        """origin looks like GitHub; pushes really go to the local bare repository."""
        sh(self.work, 'git', 'remote', 'add', 'origin', 'https://github.com/example/app.git')
        sh(self.work, 'git', 'config', 'url.%s.pushInsteadOf' % self.bare, 'https://github.com/example/app.git')

    @staticmethod
    def run_(name, status='completed', conclusion='success', job=1):
        return {'id': job, 'name': name, 'status': status, 'conclusion': conclusion, 'check_suite': {'id': 1},
                'html_url': 'https://github.com/example/app/actions/runs/9/job/%d' % job, 'output': {}}

    def build_one(self, c, fix=None):
        self.drive(c, self.waiting_on(c, 'developer'))
        with open(os.path.join(self.work, 'app.py'), 'a') as f:
            f.write(fix or 'def add(a, b):\n    return a + b\n')
        self.role(c, 'developer').reply(fenced({'status': 'done', 'summary': 'add()'}))
        self.drive(c, self.waiting_on(c, 'reviewer'))
        self.role(c, 'reviewer').reply(fenced({'verdict': 'approve', 'summary': 'ok', 'commit_message': 'feat: add'}))


class CiFlowTests(CiTestCase):
    def test_pull_request_and_ci_gate_on_github(self):
        self.github_remote()
        self.gh({'runs': [[self.run_('test', 'in_progress', None)],
                          [self.run_('test', conclusion='failure', job=41)],
                          [self.run_('test', 'in_progress', None)],
                          [self.run_('test', conclusion='success', job=42)]]})
        c = self.new(pr=True, ci=True)
        self.assertEqual(c['git'], {'forge': 'github', 'base': 'main', 'branch': 'kovan/test-crew', 'pr_url': None})
        self.assertEqual(sh(self.work, 'git', 'branch', '--show-current').strip(), 'kovan/test-crew')
        self.assertEqual(crew.pipeline(c)[-2:], ['commit', 'ci'])
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.build_one(c)
        self.drive(c, lambda c: len(self.role(c, 'developer').sent) == 2)        # the red CI went back
        dev = self.role(c, 'developer').sent[-1]
        self.assertIn('The CI failed on commit', dev)
        self.assertIn('failed: test', dev)
        self.assertIn('test_app.py:3 AssertionError: 2 != 3', dev)               # annotations …
        self.assertIn('FAILED test_app.py::test_add', dev)                       # … and the failed steps' log
        self.assertNotIn('hunter2', dev)                                         # masked
        self.assertIn('kovan/test-crew', sh(self.bare, 'git', 'branch'))          # really pushed
        texts = [b['text'] for b in c['board']]
        self.assertIn('Opened a draft pull request: https://github.com/example/app/pull/7', texts)
        self.assertEqual(c['plan']['milestones'][0]['status'], 'active')         # not done while the CI is red
        self.build_one(c, fix='# fixed\n')
        self.drive(c, lambda c: c['phase'] == 'done')
        m = c['plan']['milestones'][0]
        self.assertEqual((m['status'], m['ci']['state'], len(m['commits'])), ('done', 'green', 2))
        self.assertTrue(any(t.startswith('CI passed on ') for t in texts + [b['text'] for b in c['board']]))
        calls = self.gh()['calls']
        create = next(a for a in calls if a[:2] == ['pr', 'create'])
        self.assertEqual(create[create.index('--base') + 1], 'main')
        self.assertEqual(create[create.index('--head') + 1], 'kovan/test-crew')
        self.assertIn('--draft', create)
        self.assertIn(['pr', 'ready', 'kovan/test-crew'], calls)                 # ready for review at the end
        self.assertEqual(c['git']['pr_url'], 'https://github.com/example/app/pull/7')
        self.assertEqual(sh(self.bare, 'git', 'rev-parse', 'kovan/test-crew'), sh(self.work, 'git', 'rev-parse', 'HEAD'))

    def test_no_ci_result_blocks_and_retry_waits_again(self):
        self.github_remote()
        c = self.new(ci=True)
        self.assertEqual(c['git']['branch'], 'main')                              # no PR: the crew stays on its branch
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.build_one(c)
        self.drive(c, lambda c: c['phase'] == 'blocked')
        self.assertEqual(c['reason'], 'CI: no CI result appeared for %s within 0 minutes' %
                         sh(self.work, 'git', 'rev-parse', 'HEAD')[:8])
        self.gh({'runs': [[self.run_('build')]]})
        self.crews.act(c['id'], 'retry')
        self.assertEqual(c['step'], 'ci')
        self.drive(c, lambda c: c['phase'] == 'done')
        self.assertEqual(len(self.role(c, 'developer').sent), 1)                  # the Developer was not bothered

    def test_jenkins_status_on_github(self):
        self.github_remote()
        self.gh({'statuses': [{'context': 'continuous-integration/jenkins', 'state': 'failure',
                               'description': 'Build #12 failed: 3 tests', 'target_url': 'https://ci.example.com/12'}]})
        st = ci.github_status(self.work, 'abc123')
        self.assertEqual(st.state, 'red')
        self.assertIn('Build #12 failed: 3 tests', st.details)
        self.assertIn('https://ci.example.com/12', st.details)
        self.gh({'statuses': [{'context': 'jenkins', 'state': 'pending'}], 'runs': [[self.run_('lint')]]})
        self.assertEqual(ci.github_status(self.work, 'abc123').state, 'pending')
        self.gh({'statuses': [{'context': 'jenkins', 'state': 'success'}], 'runs': [[self.run_('lint')]]})
        self.assertEqual(ci.github_status(self.work, 'abc123').summary, 'passed: jenkins, lint')

    def test_merge_request_on_gitlab_through_push_options(self):
        hook = os.path.join(self.bare, 'hooks', 'pre-receive')
        with open(hook, 'w') as f:
            f.write('#!/bin/sh\nenv | grep GIT_PUSH_OPTION_ | sort > "%s"\n' % os.path.join(self.tmp, 'options.txt'))
        os.chmod(hook, 0o755)
        sh(self.bare, 'git', 'config', 'receive.advertisePushOptions', 'true')
        sh(self.work, 'git', 'remote', 'add', 'origin', self.bare)
        c = self.new(pr=True)
        self.assertEqual(c['git']['forge'], 'gitlab')
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.build_one(c)
        self.drive(c, lambda c: c['phase'] == 'done')
        with open(os.path.join(self.tmp, 'options.txt')) as f:
            options = f.read()
        for line in ('merge_request.create', 'merge_request.target=main', 'merge_request.title=Test crew'):
            self.assertIn(line, options)
        self.assertIn('Pushed kovan/test-crew. Open the merge request in GitLab.', [b['text'] for b in c['board']])

    def test_status_command(self):
        cmd = '%s -c "import sys; print(\'sha={sha}\'); sys.exit(int(open(\'code\').read()))"' % PY
        for code, state in (('0', 'green'), ('75', 'pending'), ('1', 'red')):
            with open(os.path.join(self.work, 'code'), 'w') as f:
                f.write(code)
            st = ci.command_status(self.work, cmd, 'deadbeef', 'main')
            self.assertEqual(st.state, state)
            self.assertIn('sha=deadbeef', st.details)

    def test_creation_rules(self):
        with self.assertRaisesRegex(server.KovanError, 'origin'):
            self.new(pr=True)
        self.github_remote()
        with self.assertRaisesRegex(server.KovanError, 'Commit each milestone'):
            self.new(ci=True, commit=False)
        with mock.patch('kovan.ci.shutil.which', return_value=None):           # no gh (CI runners have one in /usr/bin)
            with self.assertRaisesRegex(server.KovanError, 'GitHub CLI'):
                self.new(pr=True)

    def test_forge_detection(self):
        for url, forge in (('git@github.com:example/app.git', 'github'), ('https://github.com/example/app', 'github'),
                           ('ssh://git@github.com/example/app.git', 'github'), ('https://gitlab.example.edu/x/y.git', 'gitlab'),
                           ('git@gitlab.com:x/y.git', 'gitlab'), ('', None)):
            self.assertEqual(ci.forge_of(url), forge, url)


if __name__ == '__main__':
    unittest.main()
