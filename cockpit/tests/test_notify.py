"""Notification tests: a fake Telegram Bot API and a fake incoming webhook on 127.0.0.1, real crews with fake roles.

    python -m unittest discover -s tests -v
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kovan import notify, server  # noqa: E402
from test_crews import CrewTestCase, fenced  # noqa: E402

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
        cls.tg_httpd.shutdown()
        cls.hook_httpd.shutdown()

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


if __name__ == '__main__':
    unittest.main()
