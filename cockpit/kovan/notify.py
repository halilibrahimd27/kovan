"""Notifications: hear about it on your phone when a crew needs you — and answer from there.

Two optional channels, set under "notify" in ~/.config/kovan/config.json:

* webhook  — a Slack- or Mattermost-compatible incoming webhook. One way: "tablefmt needs you: approve the plan".
* telegram — a bot you create with @BotFather. Two way: the same messages with buttons (Approve, Revise, Stop,
             Retry, Skip, pick an idea, Continue, …), honoured only for the Telegram user ids you allow.
             Allowing a crew's permission prompt from the phone is off unless you turn it on.

    "notify": {
      "webhook": "https://chat.example.com/hooks/…",           (or the KOVAN_WEBHOOK_URL environment variable)
      "telegram": {"token": "…", "allow": [123456789]},       (or KOVAN_TELEGRAM_TOKEN)
      "permissions": false,                                    (true: Allow/Deny buttons for permission prompts)
      "sessions": false                                        (true: also any watched session waiting for you)
    }

The webhook URL and the bot token are secrets: kovan reads them from the config or the environment, never writes
them to its log, and masks likely secrets in what it sends. Messages carry the crew's name, what it waits for,
plan and idea titles, reasons, and the command a role wants to run.
"""
import hashlib
import ipaddress
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import server

TELEGRAM_API = 'https://api.telegram.org'
NEEDS_YOU = ('choosing', 'plan_review', 'milestone_review', 'blocked')

TR = {
    'needs you — approve the plan': 'seni bekliyor — planı onayla', 'needs you — pick an idea': 'seni bekliyor — bir fikir seç',
    'is blocked': 'takıldı', 'finished a milestone — continue?': 'bir kilometre taşını bitirdi — devam edilsin mi?',
    'needs your OK': 'onayını bekliyor', 'is done': 'bitirdi', 'needs your answer': 'cevabını bekliyor',
    'Approve': 'Onayla', 'Revise': 'Düzelt', 'Stop': 'Durdur', 'Retry': 'Yeniden dene', 'Skip': 'Geç', 'Continue': 'Devam',
    'Pause': 'Duraklat', 'More ideas': 'Başka fikirler', 'Allow': 'İzin ver', 'Deny': 'Reddet',
    'Check command': 'Denetim komutu', 'milestones': 'kilometre taşı', 'commits': 'commit’ler',
    'Answer it in kovan.': 'Cevabı kovan’da ver.', 'Done': 'Tamam', 'Not allowed': 'İzin yok',
    'This is no longer pending': 'Bu artık beklemede değil', 'Reply to this message with what should change.':
    'Bu mesaja neyin değişmesi gerektiğini yazarak cevap ver.', 'Reply with an optional hint for the next attempt '
    '(or "-" for none).': 'Sonraki deneme için istersen bir ipucu yazarak cevap ver (yoksa "-").',
    'Your Telegram user id': 'Telegram kullanıcı kimliğin', 'chat id': 'sohbet kimliği',
    'Add it to "allow" under notify.telegram in kovan\'s config.': 'Onu kovan yapılandırmasında notify.telegram altındaki '
    '"allow" listesine ekle.', 'No crews.': 'Ekip yok.', 'kovan: test notification': 'kovan: deneme bildirimi',
    'Could not do that': 'Yapılamadı', 'Plan revision sent': 'Plan düzeltmesi gönderildi', 'Retrying': 'Yeniden deneniyor',
}


class NotifyError(Exception):
    pass


def check_url(url, what):
    """https anywhere, or plain http to this machine (for local relays and tests)."""
    p = urllib.parse.urlsplit(str(url or ''))
    host = p.hostname or ''
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == 'localhost'
    if p.scheme == 'https' and host or p.scheme == 'http' and loopback:
        return str(url).rstrip('/')
    raise NotifyError('%s must be an https:// URL' % what)


def post_json(url, body, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'kovan'})
    with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 - scheme checked by check_url
        raw = r.read(2_000_000)
    try:
        return json.loads(raw) if raw.strip()[:1] in (b'{', b'[') else {}
    except ValueError:
        return {}


def failure(e):
    """Why a request failed, without the URL (a webhook URL or a bot token is in it)."""
    if isinstance(e, urllib.error.HTTPError):
        return 'HTTP %d' % e.code
    if isinstance(e, urllib.error.URLError):
        return type(e.reason).__name__ if not isinstance(e.reason, str) else 'unreachable'
    return type(e).__name__


class Settings:
    """The "notify" section of the config, validated. Secrets may come from the environment instead."""

    def __init__(self, data, environ=None, lang='en'):
        environ = os.environ if environ is None else environ
        data = data if isinstance(data, dict) else {}
        self.lang = lang if lang in ('en', 'tr') else 'en'
        url = environ.get('KOVAN_WEBHOOK_URL') or data.get('webhook')
        self.webhook = check_url(url, 'notify.webhook') if url else None
        tg = data.get('telegram') if isinstance(data.get('telegram'), dict) else {}
        self.token = environ.get('KOVAN_TELEGRAM_TOKEN') or tg.get('token') or None
        if self.token and (not isinstance(self.token, str) or ':' not in self.token or any(c.isspace() for c in self.token)):
            raise NotifyError('notify.telegram.token does not look like a bot token (123456:ABC…)')
        allow = tg.get('allow') or []
        if not isinstance(allow, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in allow):
            raise NotifyError('notify.telegram.allow must be a list of numeric Telegram user ids')
        self.allow = set(allow)
        chat = tg.get('chat', allow[0] if allow else None)
        if chat is not None and (not isinstance(chat, int) or isinstance(chat, bool)):
            raise NotifyError('notify.telegram.chat must be a numeric chat id')
        self.chat = chat
        self.api = check_url(tg.get('api') or TELEGRAM_API, 'notify.telegram.api')
        self.permissions = data.get('permissions') is True
        self.sessions = data.get('sessions') is True
        self.link = str(data.get('link') or '').strip()[:200]

    @property
    def enabled(self):
        return bool(self.webhook or self.token)

    def channels(self):
        return [n for n, on in (('webhook', self.webhook), ('telegram', self.token)) if on]


class Telegram:
    def __init__(self, settings, timeout=15):
        self.s = settings
        self.timeout = timeout

    def call(self, method, **params):
        r = post_json('%s/bot%s/%s' % (self.s.api, self.s.token, method), params, timeout=self.timeout)
        if not r.get('ok', False):
            raise NotifyError('Telegram refused %s' % method)
        return r.get('result')

    def send(self, text, buttons=None, chat=None, force_reply=False):
        params = {'chat_id': chat if chat is not None else self.s.chat, 'text': text[:4000],
                  'disable_web_page_preview': True}
        if buttons:
            params['reply_markup'] = {'inline_keyboard': buttons}
        elif force_reply:
            params['reply_markup'] = {'force_reply': True}
        return (self.call('sendMessage', **params) or {}).get('message_id')


class Message:
    def __init__(self, key, title, body='', buttons=None):
        self.key, self.title, self.body = key, title, body
        self.buttons = [row for row in buttons or [] if row]

    def text(self, link=''):
        out = self.title + ('\n\n' + self.body if self.body else '')
        return server.mask_secrets(out)[0] + ('\n\n' + link if link else '')


class Notifier:
    """Watches crews (and optionally sessions), sends messages, and answers Telegram buttons and replies."""

    def __init__(self, settings, crews, store=None, manager=None, interval=2.0):
        self.s = settings
        self.crews = crews
        self.store = store
        self.manager = manager
        self.interval = interval
        self.telegram = Telegram(settings) if settings.token else None
        self.seen = None              # crew id -> signature; None until the first look (no messages for old news)
        self.session_seen = None
        self.sent = {}                # crew id -> Telegram message ids that still show buttons
        self.replies = {}             # Telegram message id -> (crew id, action, token) waiting for a text reply
        self.offset = 0
        self.status = {name: None for name in settings.channels()}
        self.stopping = threading.Event()

    # ---- text
    def t(self, text):
        return TR.get(text, text) if self.s.lang == 'tr' else text

    @staticmethod
    def signature(c):
        return '%s|%s|%s' % (c['phase'], (c.get('attention') or {}).get('id') or '', c.get('reason') or '')

    @classmethod
    def token(cls, c):
        return hashlib.sha256(cls.signature(c).encode()).hexdigest()[:8]

    def button(self, c, label, action, arg=''):
        return {'text': self.t(label), 'callback_data': '%s|%s|%s|%s' % (c['id'], action, arg, self.token(c))}

    def crew_message(self, c, detail):
        name, phase, a = c['name'], c['phase'], c.get('attention')
        if a:
            body = '%s: %s' % (a.get('role_title') or a['role'], a['summary'])
            if self.s.permissions:
                return Message(c['id'], '⬢ %s · %s' % (name, self.t('needs your OK')), body,
                               [[self.button(c, 'Allow', 'allow'), self.button(c, 'Deny', 'deny')]])
            return Message(c['id'], '⬢ %s · %s' % (name, self.t('needs your OK')), body + '\n\n' + self.t('Answer it in kovan.'))
        if phase == 'plan_review':
            plan = detail.get('plan') or {}
            items = '\n'.join('%d. %s' % (i + 1, m['title']) for i, m in enumerate(plan.get('milestones') or []))
            check = c['options'].get('check') or plan.get('check_command')
            body = '%s\n\n%s%s' % (server.clip(plan.get('summary') or '', 700), items,
                                   ('\n\n%s: %s' % (self.t('Check command'), check)) if check else '')
            return Message(c['id'], '⬢ %s · %s' % (name, self.t('needs you — approve the plan')), body,
                           [[self.button(c, 'Approve', 'approve'), self.button(c, 'Revise', 'revise'),
                             self.button(c, 'Stop', 'stop')]])
        if phase == 'choosing':
            ideas = detail.get('ideas') or []
            body = '\n\n'.join('%d. %s (%s/100) — %s' % (i + 1, x.get('title', ''), x.get('total', '?'),
                                                         server.clip(str(x.get('summary') or ''), 220))
                               for i, x in enumerate(ideas))
            picks = [self.button(c, str(i + 1), 'pick', str(i)) for i in range(min(len(ideas), 8))]
            return Message(c['id'], '⬢ %s · %s' % (name, self.t('needs you — pick an idea')), body,
                           [picks[:4], picks[4:], [self.button(c, 'More ideas', 'more_ideas')]] if picks[4:] else
                           [picks, [self.button(c, 'More ideas', 'more_ideas')]])
        if phase == 'blocked':
            row = [self.button(c, 'Retry', 'retry')] + ([self.button(c, 'Skip', 'skip')] if detail.get('plan') else [])
            return Message(c['id'], '⬢ %s · %s' % (name, self.t('is blocked')), c.get('reason') or '',
                           [row + [self.button(c, 'Stop', 'stop')]])
        if phase == 'milestone_review':
            done = [m for m in (detail.get('plan') or {}).get('milestones') or [] if m['status'] == 'done']
            body = ('✓ %s%s' % (done[-1]['title'], ' (%s)' % done[-1]['commit'] if done[-1].get('commit') else '')) if done else ''
            return Message(c['id'], '⬢ %s · %s' % (name, self.t('finished a milestone — continue?')), body,
                           [[self.button(c, 'Continue', 'continue'), self.button(c, 'Pause', 'pause')]])
        if phase == 'done':
            plan = (detail.get('plan') or {}).get('milestones') or []
            commits = [m['commit'] for m in plan if m.get('commit')]
            body = '%d %s%s' % (sum(1 for m in plan if m['status'] == 'done'), self.t('milestones'),
                                ('; %s: %s' % (self.t('commits'), ', '.join(commits))) if commits else '')
            return Message(c['id'], '✓ %s · %s' % (name, self.t('is done')), body)
        return None

    # ---- watching
    def look(self):
        """One pass: compare every crew (and session) with the last pass and send what changed."""
        out = []
        current = {}
        with self.crews.lock:
            for c in list(self.crews.items.values()):
                summary = self.crews.summary(c)
                current[c['id']] = self.signature(summary)
                if self.seen is None or self.seen.get(c['id']) == current[c['id']]:
                    continue
                was_done = (self.seen.get(c['id']) or '').startswith('done|')
                if summary['phase'] in NEEDS_YOU or summary.get('attention') or (summary['phase'] == 'done' and not was_done):
                    if summary.get('attention'):
                        from .crew import role_title
                        summary['attention'] = dict(summary['attention'], role_title=role_title(c, summary['attention']['role']))
                    m = self.crew_message(summary, self.crews.detail(c))
                    if m:
                        out.append(m)
            changed = [cid for cid in current if self.seen is not None and self.seen.get(cid) != current[cid]]
        self.seen = current
        for cid in changed:
            self.clear_buttons(cid)
        if self.s.sessions and self.store is not None:
            out += self.session_messages()
        for m in out:
            self.deliver(m)
        return out

    def session_messages(self):
        try:
            sessions = self.store.state('', False).get('sessions') or []
        except Exception:  # noqa: BLE001 - a bad transcript must not stop notifications
            return []
        waiting = {s['sid']: s for s in sessions if s.get('status') == 'awaiting' and not s.get('crew')}
        before, self.session_seen = self.session_seen, set(waiting)
        if before is None:
            return []
        return [Message(sid, '💬 %s · %s' % (s.get('name') or sid[:8], self.t('needs your answer')))
                for sid, s in waiting.items() if sid not in before]

    def deliver(self, m):
        text = m.text(self.s.link)
        if self.s.webhook:
            try:
                post_json(self.s.webhook, {'text': text})
                self.status['webhook'] = 'ok'
            except Exception as e:  # noqa: BLE001
                self.status['webhook'] = failure(e)
                server.log('notify: webhook failed (%s)' % failure(e))
        if self.telegram and self.s.chat is not None:
            try:
                mid = self.telegram.send(text, m.buttons)
                if m.buttons and mid:
                    self.sent.setdefault(m.key, []).append(mid)
                self.status['telegram'] = 'ok'
            except Exception as e:  # noqa: BLE001
                self.status['telegram'] = failure(e)
                server.log('notify: telegram failed (%s)' % failure(e))

    def clear_buttons(self, crew_id):
        for mid in self.sent.pop(crew_id, []):
            try:
                self.telegram.call('editMessageReplyMarkup', chat_id=self.s.chat, message_id=mid,
                                   reply_markup={'inline_keyboard': []})
            except Exception:  # noqa: BLE001 - the message may be gone; nothing to do
                pass

    # ---- Telegram updates
    def poll(self, timeout=25):
        updates = self.telegram.call('getUpdates', offset=self.offset, timeout=timeout,
                                     allowed_updates=['message', 'callback_query']) or []
        for u in updates:
            self.offset = max(self.offset, int(u.get('update_id', 0)) + 1)
            try:
                if u.get('callback_query'):
                    self.on_button(u['callback_query'])
                elif u.get('message'):
                    self.on_message(u['message'])
            except Exception as e:  # noqa: BLE001 - one odd update must not stop the loop
                server.log('notify: could not handle a Telegram update (%s)' % type(e).__name__)
        return updates

    def answer(self, cq, text):
        try:
            self.telegram.call('answerCallbackQuery', callback_query_id=cq['id'], text=self.t(text))
        except Exception:  # noqa: BLE001
            pass

    def on_button(self, cq):
        uid = (cq.get('from') or {}).get('id')
        if uid not in self.s.allow:
            server.log('notify: ignored a button from a Telegram user who is not allowed')
            return self.answer(cq, 'Not allowed')
        parts = str(cq.get('data') or '').split('|')
        if len(parts) != 4:
            return self.answer(cq, 'Could not do that')
        cid, action, arg, token = parts
        msg = cq.get('message') or {}
        with self.crews.lock:
            c = self.crews.items.get(cid)
            current = c and self.token(self.crews.summary(c))
        if not c or current != token:
            self.answer(cq, 'This is no longer pending')
            return self.strip(msg)
        if action in ('revise', 'retry'):
            prompt = ('Reply to this message with what should change.' if action == 'revise' else
                      'Reply with an optional hint for the next attempt (or "-" for none).')
            mid = self.telegram.send(self.t(prompt), force_reply=True, chat=(msg.get('chat') or {}).get('id', self.s.chat))
            self.replies[mid] = (cid, action, token)
            return self.answer(cq, prompt)
        try:
            if action in ('allow', 'deny'):
                if not self.s.permissions:
                    return self.answer(cq, 'Answer it in kovan.')
                a = c.get('attention') or {}
                s = self.manager.owned(a.get('sid')) if self.manager and a else None
                if not s or a.get('id') not in s.permissions:
                    return self.answer(cq, 'This is no longer pending')
                s.answer_permission(a['id'], action, None, '' if action == 'allow' else 'Denied from Telegram.')
                server.log('notify: permission %s from Telegram for crew %s' % (action, cid))
            else:
                self.crews.act(cid, action, index=int(arg) if action == 'pick' and arg.isdigit() else None)
                server.log('notify: crew %s %s from Telegram' % (cid, action))
        except server.KovanError:
            return self.answer(cq, 'Could not do that')
        self.answer(cq, 'Done')
        self.strip(msg)

    def strip(self, msg):
        if msg.get('message_id'):
            try:
                self.telegram.call('editMessageReplyMarkup', chat_id=(msg.get('chat') or {}).get('id', self.s.chat),
                                   message_id=msg['message_id'], reply_markup={'inline_keyboard': []})
            except Exception:  # noqa: BLE001
                pass

    def on_message(self, msg):
        uid = (msg.get('from') or {}).get('id')
        chat = (msg.get('chat') or {}).get('id')
        text = str(msg.get('text') or '').strip()
        if text.split(' ')[0] in ('/start', '/id') and (not self.s.allow or uid in self.s.allow):
            # With nobody allowed yet this is how you find your id; once someone is allowed, strangers get nothing.
            self.telegram.send('%s: %s · %s: %s\n%s' % (self.t('Your Telegram user id'), uid, self.t('chat id'), chat,
                                                      '' if uid in self.s.allow else self.t(
                                                          'Add it to "allow" under notify.telegram in kovan\'s config.')),
                               chat=chat)
            return
        if uid not in self.s.allow:
            server.log('notify: ignored a message from a Telegram user who is not allowed')
            return
        if text == '/status':
            lines = ['⬢ %s — %s%s' % (c['name'], c['phase'].replace('_', ' '),
                                      ' (%d/%d)' % (c['progress']['done'], c['progress']['total']) if c['progress']['total'] else '')
                     for c in self.crews.list()]
            self.telegram.send('\n'.join(lines) or self.t('No crews.'), chat=chat)
            return
        reply_to = (msg.get('reply_to_message') or {}).get('message_id')
        if reply_to in self.replies and text:
            cid, action, token = self.replies.pop(reply_to)
            with self.crews.lock:
                c = self.crews.items.get(cid)
                current = c and self.token(self.crews.summary(c))
            if current != token:
                self.telegram.send(self.t('This is no longer pending'), chat=chat)
                return
            try:
                self.crews.act(cid, action, '' if text == '-' else text[:4000])
                server.log('notify: crew %s %s from Telegram' % (cid, action))
                self.telegram.send(self.t('Plan revision sent' if action == 'revise' else 'Retrying'), chat=chat)
            except server.KovanError:
                self.telegram.send(self.t('Could not do that'), chat=chat)

    # ---- threads
    def start(self):
        threading.Thread(target=self.watch_loop, daemon=True, name='notify-watch').start()
        if self.telegram:
            threading.Thread(target=self.poll_loop, daemon=True, name='notify-telegram').start()
        server.log('notifications on: %s' % ', '.join(self.s.channels()))

    def watch_loop(self):
        while not self.stopping.is_set():
            try:
                self.look()
            except Exception as e:  # noqa: BLE001
                server.log('notify: %s' % type(e).__name__)
            self.stopping.wait(self.interval)

    def poll_loop(self):
        backoff = 1
        while not self.stopping.is_set():
            try:
                self.poll()
                backoff = 1
            except Exception as e:  # noqa: BLE001
                self.status['telegram'] = failure(e)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 60)

    def state(self):
        return {'channels': self.s.channels(), 'status': dict(self.status), 'permissions': self.s.permissions}


def send_test(settings):
    """kovan --notify-test: one message to every configured channel. Returns {channel: 'ok' | reason}."""
    result = {}
    text = TR.get('kovan: test notification', '') if settings.lang == 'tr' else 'kovan: test notification'
    text += ' ✓'
    if settings.webhook:
        try:
            post_json(settings.webhook, {'text': text})
            result['webhook'] = 'ok'
        except Exception as e:  # noqa: BLE001
            result['webhook'] = failure(e)
    if settings.token:
        if settings.chat is None:
            result['telegram'] = 'no chat yet: send /start to your bot while kovan runs, then put your id in "allow"'
        else:
            try:
                Telegram(settings).send(text)
                result['telegram'] = 'ok'
            except Exception as e:  # noqa: BLE001
                result['telegram'] = failure(e)
    return result
