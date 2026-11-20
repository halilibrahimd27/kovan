"""Tests run against the synthetic demo data (no Claude Code installation needed).

    python -m unittest discover -s tests -v
"""
import http.client
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovan import demo, server  # noqa: E402


def setup_demo():
    info = demo.build()
    server.CFG.demo = True
    server.CFG.dirs = info['dirs']
    server.CFG.notes = info['notes']
    server.SETTINGS = server.Settings(os.path.join(info['root'], 'settings.json'), read_only=True)
    server.MANAGER = server.Manager(info['dirs'])
    server.HOLDER = None
    server.STORE = server.Store(info['claude_home'], info['dirs'], info['notes'], info['task_roots'])
    demo.attach(server.MANAGER, server.BUS, info)
    return info


class HelperTests(unittest.TestCase):
    def test_fold_keeps_length_and_ignores_case_and_accents(self):
        for s in ['İstanbul Işık', 'ÇĞÖŞÜ çğöşü', 'Crème brûlée', 'plain ascii']:
            self.assertEqual(len(server.fold(s)), len(s))
        self.assertEqual(server.fold('İSTANBUL'), 'istanbul')
        self.assertIn(server.fold('creme'), server.fold('Crème brûlée'))

    def test_mask_secrets(self):
        cases = {
            'DB_PASSWORD=hunter2': 'hunter2',
            'api_key: "abcd1234efgh"': 'abcd1234efgh',
            'Authorization: Bearer eyJabc.def.ghi': 'eyJabc.def.ghi',
            'postgres://app:s3cretpw@db:5432/x': 's3cretpw',
            'token ghp_' + 'a' * 30: 'ghp_' + 'a' * 30,
            'key AKIA' + 'B' * 16: 'AKIA' + 'B' * 16,
            '-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----': 'abc',
        }
        for text, secret in cases.items():
            masked, n = server.mask_secrets(text)
            self.assertGreater(n, 0, text)
            self.assertNotIn(secret, masked, text)
        harmless = 'Ran 24 tests in 3.4s — all green; see src/password_reset.py for the flow'
        self.assertEqual(server.mask_secrets(harmless), (harmless, 0))

    def test_project_folder_encoding(self):
        self.assertTrue(server.project_folder('/h', '/Users/x/my.project').endswith('projects/-Users-x-my-project'))


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.info = setup_demo()
        cls.state = server.STORE.state('', True)
        cls.by_name = {s['name']: s for s in cls.state['sessions']}

    def test_sessions_places_and_statuses(self):
        self.assertEqual(len(self.state['sessions']), 8)
        self.assertEqual(self.by_name['API refactor']['place'], 'kovan')
        self.assertEqual(self.by_name['Release prep']['status'], 'awaiting')
        self.assertEqual(self.by_name['Infra']['place'], 'vscode')
        self.assertEqual(self.by_name['Infra']['status'], 'working')
        self.assertEqual(self.by_name['Docs']['place'], 'elsewhere')
        self.assertTrue(self.by_name['Onboarding notes']['archived'])

    def test_messages_are_paired_with_their_receivers(self):
        msgs = [m for m in self.state['messages'] if not m['subagent']]
        self.assertEqual(len(msgs), 3)
        self.assertTrue(all(m['delivered'] for m in msgs))
        api = self.by_name['API refactor']['sid']
        self.assertTrue(any(m['to'] == api for m in msgs))

    def test_tool_results_are_not_kept_in_memory(self):
        for t in server.STORE.transcripts.values():
            for ev in t.events:
                self.assertNotIn('hunter2', json.dumps(ev))

    def test_tool_detail_masks_secrets_and_returns_diffs(self):
        sid = self.by_name['Security review']['sid']
        events = server.STORE.session(sid, '')['events']
        bash = next(e for e in events if e['kind'] == 'tool' and e['tool'] == 'Bash')
        detail = server.STORE.tool_detail(sid, bash['tid'])
        self.assertGreaterEqual(detail['masked'], 3)
        self.assertNotIn('hunter2', detail['result']['text'])
        self.assertIn('••••', detail['result']['text'])
        edit = next(e for e in events if e['kind'] == 'tool' and e['tool'] == 'Edit')
        patch = server.STORE.tool_detail(sid, edit['tid'])['result']['patch']
        self.assertTrue(any(line.startswith('+') for line in patch[0]['lines']))

    def test_background_task_output(self):
        sid = self.by_name['Infra']['sid']
        detail = server.STORE.session(sid, '')
        self.assertEqual(detail['tasks'][0]['status'], 'running')
        out = server.STORE.task_output(sid, detail['tasks'][0]['id'])
        self.assertIn('GET /customers', out['text'])
        with self.assertRaises(server.KovanError):
            server.STORE.task_output(sid, '../../etc/passwd')

    def test_search_finds_messages_and_notes(self):
        results = server.STORE.search('staging')['results']
        kinds = {r['kind'] for r in results}
        self.assertIn('note', kinds)
        self.assertTrue(kinds & {'prompt', 'reply', 'sent', 'received', 'tool'})
        hit = next(r for r in results if r['sid'])
        self.assertEqual(hit['match'].lower(), 'staging')

    def test_unchanged_revision(self):
        rev = server.STORE.state('', False)['revision']
        self.assertEqual(server.STORE.state(rev, False), {'unchanged': True})

    def test_notes(self):
        notes = server.STORE.read_notes()
        self.assertEqual([n['kind'] for n in notes], ['journal', 'doc'])
        self.assertTrue(notes[0]['entries'][0]['heading'].startswith('Thu'))   # newest first


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.info = setup_demo()
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.httpd.allowed_hosts = {'127.0.0.1:%d' % cls.port}
        cls.httpd.allowed_origins = {'http://127.0.0.1:%d' % cls.port}
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': '127.0.0.1:%d' % self.port}
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def post(self, path, body, token=True, origin=True, extra=None):
        h = {'Content-Type': 'application/json'}
        if token:
            h['X-Kovan-Token'] = server.TOKEN
        if origin:
            h['Origin'] = 'http://127.0.0.1:%d' % self.port
        h.update(extra or {})
        return self.request('POST', path, body, h)

    def test_page_and_state(self):
        status, page = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(server.TOKEN.encode(), page)
        status, data = self.request('GET', '/api/state')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)['demo'])

    def test_rejects_foreign_host(self):
        self.assertEqual(self.request('GET', '/api/state', headers={'Host': 'evil.example:%d' % self.port})[0], 403)

    def test_rejects_cross_site_reads(self):
        self.assertEqual(self.request('GET', '/api/state', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_post_requires_token_origin_and_json(self):
        body = {'sid': self.info['ids']['api'], 'archived': True}
        self.assertEqual(self.post('/api/archive', body, token=False)[0], 403)
        self.assertEqual(self.post('/api/archive', body, origin=False)[0], 403)
        self.assertEqual(self.post('/api/archive', body, extra={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.post('/api/archive', body, extra={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.post('/api/archive', body, extra={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.post('/api/archive', body)[0], 200)

    def test_demo_is_read_only(self):
        status, data = self.post('/api/send', {'sid': self.info['ids']['api'], 'text': 'hi'})
        self.assertEqual(status, 403)
        self.assertIn('read-only', json.loads(data)['error'])

    def test_rejects_invalid_ids(self):
        self.assertEqual(self.request('GET', '/api/session?id=../../x')[0], 400)
        self.assertEqual(self.request('GET', '/api/tool?sid=nope&tid=x')[0], 400)

    def test_events_stream_opens(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('GET', '/api/events', headers={'Host': '127.0.0.1:%d' % self.port})
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn('text/event-stream', r.getheader('Content-Type'))
        self.assertTrue(r.fp.readline().startswith(b'retry:'))
        conn.close()


if __name__ == '__main__':
    unittest.main()
