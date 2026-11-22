"""Notification tests: a fake Telegram Bot API and a fake incoming webhook on 127.0.0.1, real crews with fake roles.

    python -m unittest discover -s tests -v
"""
import json
import os
import stat
import sys
import threading
import time
import unittest
from unittest import mock  # noqa: F401 - unittest.mock below
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovan import notify, server  # noqa: E402
from test_crews import CrewTestCase, fenced, PY  # noqa: E402

TOKEN = '123456:TEST-token_abc'
ME, STRANGER = 4242, 999


class FakeTelegram(BaseHTTPRequestHandler):
    calls, updates, next_id = [], [], [100]

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
        if not self.path.startswith('/bot%s/' % TOKEN):
            return self.reply(401, {'ok': False, 'description': 'Unauthorized'})
        method = self.path.rsplit('/', 1)[1]
        FakeTelegram.calls.append((method, body))
        if method == 'getUpdates':
            out, FakeTelegram.updates[:] = list(FakeTelegram.updates), []
            return self.reply(200, {'ok': True, 'result': out})
        if method == 'sendMessage':
            FakeTelegram.next_id[0] += 1
            return self.reply(200, {'ok': True, 'result': {'message_id': FakeTelegram.next_id[0]}})
        return self.reply(200, {'ok': True, 'result': True})

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeWebhook(BaseHTTPRequestHandler):
    posts = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        FakeWebhook.posts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')


FAKE_CLAUDE = r"""#!%s
# A stand-in for Claude Code in stream-json mode: Remote Control, PushNotification, and what kovan answers.
import json, os, sys
log = open(os.environ['FAKE_CLAUDE_LOG'], 'a')
mode = os.environ.get('FAKE_PUSH_MODE', 'ok')
def note(**kw):
    log.write(json.dumps(kw) + '\n'); log.flush()
def out(obj):
    sys.stdout.write(json.dumps(obj) + '\n'); sys.stdout.flush()
note(event='start', args=sys.argv[1:], env={k: v for k, v in os.environ.items() if k.startswith(('KOVAN', 'CLAUDE', 'ENABLE'))},
     cwd=os.getcwd())
for line in sys.stdin:
    m = json.loads(line)
    if m['type'] == 'control_request':
        req, rid = m['request'], m['request_id']
        if req['subtype'] == 'initialize':
            out({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': rid, 'response': {'commands': []}}})
        elif req['subtype'] == 'remote_control':
            note(event='remote_control', request=req)
            if req.get('reattach_session_id') and os.environ.get('FAKE_RC_FAIL') == 'reattach':
                out({'type': 'control_response', 'response': {'subtype': 'error', 'request_id': rid, 'error': 'session not found'}})
                continue
            out({'type': 'system', 'subtype': 'bridge_state', 'state': 'ready'})
            out({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': rid, 'response': {
                'session_url': 'https://claude.ai/code/session_TEST', 'bridge_session_id': 'cse_TEST'}}})
            out({'type': 'system', 'subtype': 'bridge_state', 'state': 'connected'})
    elif m['type'] == 'control_response':
        note(event='host_answer', response=m['response'])
    elif m['type'] == 'user':
        text = m['message']['content']
        note(event='user', text=text)
        push = text.split('\n', 1)[0][len('PUSH: '):]
        if mode == 'other_tool':
            out({'type': 'control_request', 'request_id': 'q1', 'request': {'subtype': 'can_use_tool', 'tool_name': 'Bash',
                                                                           'input': {'command': 'ls'}}})
            note(event='host_answer', response=json.loads(sys.stdin.readline())['response'])
        if mode == 'error':
            out({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'result': 'usage limit reached'})
            continue
        if mode == 'silent':
            out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'sent'}]}})
            out({'type': 'result', 'subtype': 'success', 'is_error': False})
            continue
        answer = {'ok': 'Mobile push requested.', 'other_tool': 'Mobile push requested.',
                  'config_off': 'Push not sent — mobile push is disabled in /config.'}[mode]
        out({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'PushNotification',
                                                           'input': {'message': push, 'status': 'proactive'}}]}})
        out({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': answer}]}})
        out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'sent'}]}})
        out({'type': 'result', 'subtype': 'success', 'is_error': False})
"""


def serve(handler):
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, 'http://127.0.0.1:%d' % httpd.server_address[1]


class SettingsTests(unittest.TestCase):
    def test_validation_and_secrets_from_the_environment(self):
        s = notify.Settings({'telegram': {'allow': [ME]}}, environ={'KOVAN_TELEGRAM_TOKEN': TOKEN,
                                                                     'KOVAN_WEBHOOK_URL': 'https://chat.example.com/hooks/x'})
        self.assertEqual((s.token, s.chat, s.allow, s.webhook), (TOKEN, ME, {ME}, 'https://chat.example.com/hooks/x'))
        self.assertEqual(s.channels(), ['webhook', 'telegram'])
        self.assertFalse(s.permissions)                                    # phone approvals of commands are opt-in
        self.assertFalse(notify.Settings(None, environ={}).enabled)
        for data, message in (({'webhook': 'http://chat.example.com/hooks/x'}, 'https'),
                              ({'webhook': 'file:///etc/passwd'}, 'https'),
                              ({'telegram': {'token': 'nope'}}, 'bot token'),
                              ({'telegram': {'token': TOKEN, 'allow': ['4242']}}, 'numeric'),
                              ({'telegram': {'token': TOKEN, 'allow': [True]}}, 'numeric'),
                              ({'telegram': {'token': TOKEN, 'api': 'http://evil.example'}}, 'https')):
            with self.assertRaisesRegex(notify.NotifyError, message):
                notify.Settings(data, environ={})
        self.assertEqual(notify.Settings({'webhook': 'http://127.0.0.1:9/x'}, environ={}).webhook, 'http://127.0.0.1:9/x')

    def test_failures_never_show_the_url(self):
        import urllib.error
        e = urllib.error.HTTPError('https://api.telegram.org/bot%s/sendMessage' % TOKEN, 404, 'Not Found', {}, None)
        self.assertEqual(notify.failure(e), 'HTTP 404')
        self.assertNotIn(TOKEN, notify.failure(urllib.error.URLError('bad host')))


class NotifierTests(CrewTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tg_httpd, cls.tg_url = serve(FakeTelegram)
        cls.hook_httpd, cls.hook_url = serve(FakeWebhook)

    @classmethod
    def tearDownClass(cls):
        for httpd in (cls.tg_httpd, cls.hook_httpd):
            httpd.shutdown()
            httpd.server_close()

    def setUp(self):
        super().setUp()
        FakeTelegram.calls[:], FakeTelegram.updates[:], FakeWebhook.posts[:] = [], [], []
        self.logged = []
        self.saved_log = server.log
        server.log = self.logged.append

    def tearDown(self):
        server.log = self.saved_log
        super().tearDown()

    def notifier(self, **extra):
        data = {'webhook': self.hook_url + '/hooks/secret-path', 'telegram': {'token': TOKEN, 'allow': [ME], 'api': self.tg_url}}
        data.update(extra)
        n = notify.Notifier(notify.Settings(data, environ={}, lang=extra.pop('lang', 'en')), self.crews,
                            manager=server.MANAGER)
        n.look()                                                          # the first look only records the state
        return n

    def sent(self, method='sendMessage'):
        return [b for m, b in FakeTelegram.calls if m == method]

    def press(self, n, data, user=ME, message_id=None):
        FakeTelegram.updates.append({'update_id': len(FakeTelegram.calls) + 1, 'callback_query': {
            'id': 'cb%d' % len(FakeTelegram.calls), 'from': {'id': user}, 'data': data,
            'message': {'message_id': message_id or FakeTelegram.next_id[0], 'chat': {'id': ME}}}})
        n.poll(timeout=0)
        return self.sent('answerCallbackQuery')[-1]['text']

    def button(self, label):
        for row in self.sent()[-1]['reply_markup']['inline_keyboard']:
            for b in row:
                if b['text'] == label:
                    return b['callback_data']
        self.fail('no %r button in %s' % (label, self.sent()[-1]))

    def to_plan_review(self, **opts):
        c = self.new(plan_approval=True, **opts)
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(2)))
        self.drive(c, lambda c: c['phase'] == 'plan_review')
        return c

    def test_plan_approval_from_the_phone(self):
        n = self.notifier()
        c = self.to_plan_review()
        self.assertEqual(len(n.look()), 1)
        msg = self.sent()[-1]
        self.assertEqual(msg['chat_id'], ME)
        self.assertIn('Test crew · needs you — approve the plan', msg['text'])
        self.assertIn('1. Step 1\n2. Step 2', msg['text'])
        self.assertEqual(FakeWebhook.posts[-1]['text'], msg['text'])      # the same text reaches the webhook
        self.assertEqual(n.look(), [])                                     # nothing new: no repeat
        self.assertEqual(self.press(n, self.button('Approve')), 'Done')
        self.assertEqual(c['phase'], 'building')
        self.assertEqual(self.sent('editMessageReplyMarkup')[-1]['reply_markup'], {'inline_keyboard': []})
        self.assertIn('notify: crew %s approve from Telegram' % c['id'], self.logged)

    def test_strangers_and_stale_buttons_are_refused(self):
        n = self.notifier()
        c = self.to_plan_review()
        n.look()
        approve = self.button('Approve')
        self.assertEqual(self.press(n, approve, user=STRANGER), 'Not allowed')
        self.assertEqual(c['phase'], 'plan_review')
        self.crews.act(c['id'], 'stop')                                    # answered in kovan meanwhile
        self.assertEqual(self.press(n, approve), 'This is no longer pending')
        self.assertEqual(c['phase'], 'stopped')
        self.assertEqual(self.press(n, 'garbage'), 'Could not do that')

    def test_revise_by_replying(self):
        n = self.notifier()
        c = self.to_plan_review()
        n.look()
        self.press(n, self.button('Revise'))
        prompt = self.sent()[-1]
        self.assertEqual(prompt['reply_markup'], {'force_reply': True})
        FakeTelegram.updates.append({'update_id': 900, 'message': {
            'message_id': 7, 'from': {'id': ME}, 'chat': {'id': ME}, 'text': 'Smaller steps, please.',
            'reply_to_message': {'message_id': FakeTelegram.next_id[0]}}})
        n.poll(timeout=0)
        self.assertEqual((c['phase'], c['plan_feedback']), ('planning', 'Smaller steps, please.'))
        self.assertEqual(self.sent()[-1]['text'], 'Plan revision sent')

    def test_pick_an_idea_and_turkish_text(self):
        n = self.notifier(lang='tr')
        c = self.crews.create('Fikir', '', 'cli tools', 'ideas', self.work, {'plan_approval': True})
        self.drive(c, self.waiting_on(c, 'ideator'))
        self.role(c, 'ideator').reply(fenced({'ideas': [{'title': 'A', 'summary': 'a', 'total': 80},
                                                        {'title': 'B', 'summary': 'b', 'total': 70}]}))
        self.drive(c, lambda c: c['phase'] == 'choosing')
        n.look()
        self.assertIn('Fikir · seni bekliyor — bir fikir seç', self.sent()[-1]['text'])
        self.assertIn('2. B (70/100) — b', self.sent()[-1]['text'])
        self.press(n, self.button('2'))
        self.assertEqual(c['phase'], 'planning')
        self.assertTrue(c['goal'].startswith('B — '))

    def test_blocked_retry_with_a_hint_and_done(self):
        n = self.notifier()
        c = self.new(max_attempts=1)
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.drive(c, self.waiting_on(c, 'developer'))
        self.role(c, 'developer').reply(fenced({'status': 'blocked', 'summary': 's', 'blocker': 'need DB_PASSWORD=hunter2'}))
        self.drive(c, lambda c: c['phase'] == 'blocked')
        n.look()
        self.assertIn('is blocked', self.sent()[-1]['text'])
        self.assertNotIn('hunter2', self.sent()[-1]['text'])               # likely secrets are masked
        self.assertNotIn('hunter2', json.dumps(FakeWebhook.posts))
        self.press(n, self.button('Retry'))
        FakeTelegram.updates.append({'update_id': 901, 'message': {
            'message_id': 8, 'from': {'id': ME}, 'chat': {'id': ME}, 'text': 'Use SQLite instead.',
            'reply_to_message': {'message_id': FakeTelegram.next_id[0]}}})
        n.poll(timeout=0)
        self.assertEqual(c['phase'], 'building')
        self.drive(c, self.waiting_on(c, 'developer'))
        self.assertIn('Use SQLite instead.', self.role(c, 'developer').sent[-1])
        self.role(c, 'developer').reply(fenced({'status': 'done', 'summary': 'ok'}))
        self.drive(c, self.waiting_on(c, 'reviewer'))
        self.role(c, 'reviewer').reply(fenced({'verdict': 'approve', 'summary': 'ok', 'commit_message': 'feat: x'}))
        self.drive(c, lambda c: c['phase'] == 'done')
        n.look()
        self.assertIn('Test crew · is done', FakeWebhook.posts[-1]['text'])

    def test_permission_prompts_need_opting_in(self):
        for allowed in (False, True):
            n = self.notifier(permissions=allowed)
            c = self.new(policy='safe')
            self.drive(c, self.waiting_on(c, 'planner'))
            planner = self.role(c, 'planner')
            planner.permissions['p1'] = {'tool': 'Bash', 'input': {'command': 'curl https://example.com'}}
            self.drive(c, lambda c: c.get('attention'))
            n.look()
            text = self.sent()[-1]
            self.assertIn('$ curl https://example.com', text['text'])
            if not allowed:
                self.assertNotIn('reply_markup', text)
                self.assertIn('Answer it in kovan.', text['text'])
            else:
                self.assertEqual(self.press(n, self.button('Allow')), 'Done')
                self.assertEqual(planner.answers[-1], ('p1', 'allow'))
            self.crews.act(c['id'], 'delete')

    def test_start_tells_you_your_id_only_while_nobody_is_allowed(self):
        n = notify.Notifier(notify.Settings({'telegram': {'token': TOKEN, 'api': self.tg_url}}, environ={}), self.crews)
        FakeTelegram.updates.append({'update_id': 1, 'message': {'message_id': 1, 'from': {'id': STRANGER},
                                                                 'chat': {'id': 77}, 'text': '/start'}})
        n.poll(timeout=0)
        self.assertIn('Your Telegram user id: %d · chat id: 77' % STRANGER, self.sent()[-1]['text'])
        n = self.notifier()
        before = len(self.sent())
        FakeTelegram.updates.append({'update_id': 2, 'message': {'message_id': 2, 'from': {'id': STRANGER},
                                                                 'chat': {'id': 77}, 'text': '/start'}})
        FakeTelegram.updates.append({'update_id': 3, 'message': {'message_id': 3, 'from': {'id': STRANGER},
                                                                 'chat': {'id': 77}, 'text': '/status'}})
        n.poll(timeout=0)
        self.assertEqual(len(self.sent()), before)                         # strangers get nothing
        FakeTelegram.updates.append({'update_id': 4, 'message': {'message_id': 4, 'from': {'id': ME},
                                                                 'chat': {'id': ME}, 'text': '/status'}})
        n.poll(timeout=0)
        self.assertEqual(self.sent()[-1]['text'], 'No crews.')

    def test_secrets_never_reach_the_log(self):
        n = self.notifier(webhook='http://127.0.0.1:9/hooks/secret-path')   # nothing listens there
        c = self.to_plan_review()
        n.look()
        self.assertEqual(n.status['webhook'], 'ConnectionRefusedError')
        joined = '\n'.join(self.logged)
        self.assertIn('notify: webhook failed (ConnectionRefusedError)', joined)
        for secret in (TOKEN, 'secret-path'):
            self.assertNotIn(secret, joined)
        self.assertEqual(c['phase'], 'plan_review')

    def test_send_test(self):
        s = notify.Settings({'webhook': self.hook_url + '/x', 'telegram': {'token': TOKEN, 'api': self.tg_url}}, environ={})
        self.assertEqual(notify.send_test(s)['webhook'], 'ok')
        self.assertIn('no chat yet', notify.send_test(s)['telegram'])
        s = notify.Settings({'telegram': {'token': TOKEN, 'allow': [ME], 'api': self.tg_url}}, environ={})
        self.assertEqual(notify.send_test(s), {'telegram': 'ok'})


class ClaudeChannelTests(CrewTestCase):
    """notify.claude: pushes through a (fake) Claude Code session's PushNotification tool over Remote Control."""

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, 'claude')
        with open(self.bin, 'w') as f:
            f.write(FAKE_CLAUDE % PY)
        os.chmod(self.bin, 0o755)
        self.log_path = os.path.join(self.tmp, 'claude.log')
        self.saved_env = {k: os.environ.get(k) for k in ('FAKE_CLAUDE_LOG', 'FAKE_PUSH_MODE', 'FAKE_RC_FAIL',
                                                         'KOVAN_TELEGRAM_TOKEN')}
        os.environ['FAKE_CLAUDE_LOG'] = self.log_path
        os.environ['KOVAN_TELEGRAM_TOKEN'] = TOKEN          # must never reach the relay session
        self.logged = []
        self.saved_log, server.log = server.log, self.logged.append
        self.notifiers = []

    def tearDown(self):
        for n in self.notifiers:
            n.stopping.set()
            n.relay.close()
        server.log = self.saved_log
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def notifier(self, claude=True, **extra):
        n = notify.Notifier(notify.Settings(dict(extra, claude=claude), environ={}, lang=extra.get('lang', 'en')), self.crews)
        n.relay = notify.ClaudeRelay(n.s, state_dir=self.state, binary=self.bin, timeout=10,
                                     claude_home=os.path.join(self.tmp, 'home'))
        self.notifiers.append(n)
        n.look()
        return n

    def calls(self, event=None):
        with open(self.log_path) as f:
            items = [json.loads(x) for x in f]
        return [x for x in items if event is None or x['event'] == event]

    def to_plan_review(self):
        c = self.new(plan_approval=True)
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(2)))
        self.drive(c, lambda c: c['phase'] == 'plan_review')
        return c

    def test_a_crew_waiting_for_you_becomes_a_claude_push(self):
        n = self.notifier()
        self.to_plan_review()
        self.assertEqual(len(n.look()), 1)
        self.assertEqual(n.status['claude'], 'ok')
        start = self.calls('start')[0]
        args = start['args']
        for flag in (['--tools', 'PushNotification'], ['--model', 'haiku'], ['--name', 'kovan']):
            i = args.index(flag[0])
            self.assertEqual(args[i:i + 2], flag)
        self.assertIn('--strict-mcp-config', args)
        self.assertIn('--setting-sources=user', args)
        self.assertEqual(start['env'].get('CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK'), '1')
        self.assertEqual(start['env'].get('ENABLE_CLAUDEAI_MCP_SERVERS'), 'false')
        self.assertFalse([k for k in start['env'] if k.startswith('KOVAN')])          # no kovan secrets in there
        self.assertEqual(os.path.realpath(start['cwd']), os.path.realpath(os.path.join(self.state, 'notify-claude')))
        rc = self.calls('remote_control')[0]['request']
        self.assertEqual((rc['enabled'], rc['name'], rc['keep_session_on_exit']), (True, 'kovan', True))
        self.assertNotIn('reattach_session_id', rc)
        text = self.calls('user')[0]['text']
        self.assertTrue(text.startswith('PUSH: ⬢ Test crew · needs you — approve the plan: Two small steps.\n\n'), text)
        self.assertIn('1. Step 1\n2. Step 2', text)                                   # the details stay in the chat
        self.assertEqual(n.state()['claude_url'], 'https://claude.ai/code/session_TEST')
        saved = os.path.join(self.state, 'notify-claude', 'remote.json')
        self.assertEqual(stat.S_IMODE(os.stat(saved).st_mode), 0o600)
        self.assertEqual(n.look(), [])                                                # nothing new: no repeat
        self.assertEqual(len(self.calls('user')), 1)

    def test_the_same_claude_conversation_after_a_restart(self):
        n = self.notifier()
        first = self.to_plan_review()
        n.look()
        n.relay.close()
        self.crews.act(first['id'], 'delete')
        n2 = self.notifier()
        c = self.new(max_attempts=1)
        self.drive(c, self.waiting_on(c, 'planner'))
        self.role(c, 'planner').reply(fenced(self.plan(1)))
        self.drive(c, self.waiting_on(c, 'developer'))
        self.role(c, 'developer').reply(fenced({'status': 'blocked', 'summary': 's', 'blocker': 'need DB_PASSWORD=hunter2'}))
        self.drive(c, lambda c: c['phase'] == 'blocked')
        n2.look()
        self.assertEqual(n2.status['claude'], 'ok')
        self.assertEqual(self.calls('remote_control')[-1]['request'].get('reattach_session_id'), 'cse_TEST')
        line = self.calls('user')[-1]['text'].split('\n')[0]
        self.assertTrue(line.startswith('PUSH: ⬢ Test crew · is blocked'), line)
        self.assertNotIn('hunter2', json.dumps(self.calls('user')))                   # likely secrets are masked
        os.environ['FAKE_RC_FAIL'] = 'reattach'                                       # that conversation was deleted
        n2.relay.close()
        self.crews.act(c['id'], 'stop')
        self.assertEqual(n2.relay.push('⬢ x · is done'), 'ok')
        last = [x['request'] for x in self.calls('remote_control')[-2:]]
        self.assertEqual((last[0].get('reattach_session_id'), 'reattach_session_id' in last[1]), ('cse_TEST', False))

    def test_why_a_push_did_not_go_out(self):
        n = self.notifier()
        for mode, status in (('config_off', 'turn on “Push when Claude decides” in Claude Code’s /config'),
                             ('silent', 'Claude did not send it'), ('error', 'Claude: usage limit reached'),
                             ('other_tool', 'ok')):
            os.environ['FAKE_PUSH_MODE'] = mode
            n.relay.close()
            self.assertEqual(n.relay.push('⬢ Test crew · is done'), status, mode)
        denied = self.calls('host_answer')[-1]['response']
        self.assertEqual(denied['response']['behavior'], 'deny')                      # only PushNotification may run
        missing = notify.ClaudeRelay(n.s, state_dir=self.state, binary=None)
        with unittest.mock.patch('kovan.server.find_claude', return_value=None):
            self.assertEqual(missing.push('x'), 'could not find the claude executable')

    def test_pushes_go_out_from_their_own_thread_and_the_relay_rests_when_idle(self):
        saved = (notify.CLAUDE_POLL, notify.CLAUDE_IDLE_SECONDS)
        notify.CLAUDE_POLL, notify.CLAUDE_IDLE_SECONDS = 0.05, 0.3
        try:
            n = self.notifier()
            n.start()
            self.to_plan_review()
            n.look()
            end = time.time() + 10
            while n.status.get('claude') != 'ok' and time.time() < end:
                time.sleep(0.05)
            self.assertEqual(n.status['claude'], 'ok')
            while n.relay.alive() and time.time() < end:
                time.sleep(0.05)
            self.assertFalse(n.relay.alive())                                         # exited after the quiet spell
        finally:
            notify.CLAUDE_POLL, notify.CLAUDE_IDLE_SECONDS = saved

    def test_settings_and_the_push_line(self):
        s = notify.Settings({'claude': {'model': 'sonnet', 'name': 'kovan bildirimleri'}}, environ={})
        self.assertEqual((s.claude, s.channels(), s.enabled), ({'model': 'sonnet', 'name': 'kovan bildirimleri'}, ['claude'], True))
        self.assertIsNone(notify.Settings({'claude': {'enabled': False}}, environ={}).claude)
        self.assertIsNone(notify.Settings({'claude': False}, environ={}).claude)
        for bad, message in (('yes', 'true, false'), ({'model': 'haiku; rm -rf /'}, 'model'), ({'name': ''}, 'name'),
                             ({'name': 'a\nb'}, 'name')):
            with self.assertRaisesRegex(notify.NotifyError, message):
                notify.Settings({'claude': bad}, environ={})
        m = notify.Message('c1', '⬢ shop · is blocked', 'token=ghp_' + 'a' * 36 + ' was rejected\nsecond line')
        line = m.line()
        self.assertNotIn('\n', line)
        self.assertNotIn('ghp_aaaa', line)
        self.assertTrue(line.startswith('⬢ shop · is blocked: '))
        self.assertLessEqual(len(notify.Message('c', 'x' * 300).line()), 180)
        self.assertEqual(notify.Message('c', '⬢ shop · needs you — approve the plan', 'y' * 200 + '\n1. A').line(),
                         '⬢ shop · needs you — approve the plan')                  # a long summary stays in the chat


if __name__ == '__main__':
    unittest.main()
