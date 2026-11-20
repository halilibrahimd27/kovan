"""Read-mostly client for the swarm engine's API (src/aidevswarm/api), so its boardroom shows up in kovan.

The swarm API listens on loopback only and guards writes with an Origin check plus an optional bearer
token (AIDEVSWARM_API_TOKEN). kovan talks to it server-side, so the browser never sees the token and the
swarm stays unreachable from the page itself. Only loopback URLs are accepted: the token is never sent to
another host, and kovan cannot be turned into a proxy for anything else.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import server

LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
# Non-destructive operator commands only. Aborting a project stays in the swarm's own UI and Telegram,
# where it asks for confirmation.
INTENTS = ('inject_note', 'approve', 'pause_project', 'resume_project')
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def check_url(url):
    u = urllib.parse.urlsplit(str(url or '').strip())
    if u.scheme != 'http' or (u.hostname or '') not in LOOPBACK or u.username or u.password or u.query or u.fragment:
        raise ValueError('the swarm URL must be a plain http:// URL on 127.0.0.1 or localhost, e.g. http://127.0.0.1:8080')
    return '%s://%s%s' % (u.scheme, u.netloc, u.path.rstrip('/'))


def valid_project(pid):
    pid = str(pid or '').lower()
    if not UUID_RE.match(pid):
        raise server.KovanError('invalid swarm project id')
    return pid


class Swarm:
    def __init__(self, url, token=None, timeout=6):
        self.url = check_url(url)
        self.token = token or None
        self.timeout = timeout
        self.error = None

    def request(self, method, path, body=None):
        data = json.dumps(body).encode('utf-8') if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header('Accept', 'application/json')
        if data is not None:
            req.add_header('Content-Type', 'application/json')
        if self.token:
            req.add_header('Authorization', 'Bearer ' + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:   # noqa: S310 - loopback http only (check_url)
                payload = json.loads(r.read(8 * 1024 * 1024) or b'null')
            self.error = None
            return payload
        except urllib.error.HTTPError as e:
            self.error = 'the swarm API answered %d' % e.code + (' (check KOVAN_SWARM_TOKEN)' if e.code == 401 else '')
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.error = 'the swarm API is not reachable at %s (%s)' % (self.url, getattr(e, 'reason', None) or type(e).__name__)
        raise server.KovanError(self.error, 502)

    # ---- reads
    def projects(self):
        data = self.request('GET', '/api/dashboard')
        out = []
        for p in (data or {}).get('projects') or []:
            if isinstance(p, dict) and UUID_RE.match(str(p.get('id', ''))):
                out.append({'id': p['id'], 'name': str(p.get('name') or '')[:120], 'state': str(p.get('state') or ''),
                            'detail': server.clip(str(p.get('status_detail') or ''), 300),
                            'repo': str(p.get('github_repo') or '')[:200],
                            'done': int(p.get('done') or 0), 'total': int(p.get('total') or 0),
                            'cost': float(p.get('cost') or 0)})
        return out

    def board(self, project_id, everything=False):
        """The meeting: decisions only (the swarm's boardroom), or the whole transcript."""
        pid = valid_project(project_id)
        path = '/api/transcript/%s?limit=600' % pid if everything else '/api/boardroom/%s' % pid
        out = []
        for e in self.request('GET', path) or []:
            if not isinstance(e, dict):
                continue
            text, _ = server.mask_secrets(server.clip(str(e.get('text') or ''), 6000))
            out.append({'id': str(e.get('id') or ''), 'ts': server.ts_ms(e.get('at')), 'speaker': str(e.get('role') or '')[:40],
                        'kind': str(e.get('kind') or ''), 'text': text})
        return out

    # ---- writes
    def command(self, intent, project_id, body=None, role=None):
        if intent not in INTENTS:
            raise server.KovanError('kovan only sends non-destructive swarm commands', 400)
        cmd = {'intent': intent, 'project_id': str(uuid.UUID(valid_project(project_id)))}
        if intent == 'inject_note':
            body = str(body or '').strip()[:4000]
            if not body:
                raise server.KovanError('empty note')
            cmd.update(body=body, role=(str(role)[:40] or None) if role else None, author='human')
        result = self.request('POST', '/api/commands', cmd)
        server.log('action: swarm %s' % intent)
        return result if isinstance(result, dict) else {'ok': True}


class DemoSwarm:
    """Synthetic swarm data for --demo and the screenshots."""

    def __init__(self, info):
        self.data = info
        self.url = 'http://127.0.0.1:8080'
        self.error = None

    def projects(self):
        return self.data['projects']

    def board(self, project_id, everything=False):
        entries = self.data['boards'].get(valid_project(project_id))
        if entries is None:
            raise server.KovanError('swarm project not found', 404)
        return entries if everything else [e for e in entries if e['kind'] == 'decision']

    def command(self, intent, project_id, body=None, role=None):
        raise server.KovanError('Demo mode is read-only.', 403)
