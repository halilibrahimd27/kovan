"""kovan holder: a tiny, stable process that owns the Claude Code processes started from kovan.

The web server can restart (upgrade, crash, ``kovan --stop``) without killing your sessions,
because the ``claude`` child processes live here instead. kovan starts the holder on demand.

It only listens on a Unix socket (never on the network):
    <state dir>/holder-<port>.sock      (directory 0700, socket 0600 — current user only)

Wire format: one JSON object per line.
    client -> holder : hello | start | write | close | subscribe | close_all | shutdown
    holder -> client : hello | started | error | session | output | ready | exited | ok

When a client subscribes, the holder replays for every live session its "sticky" lines
(initialize response, init, title, rate limit) plus every line since the last ``result`` —
including permission requests that are still unanswered — so a restarted server knows exactly
where each session is. With no sessions and no clients for 10 minutes it exits on its own.

Keep this file small and boring: changing it means restarting the holder, which ends sessions.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

PROTOCOL = 1
IDLE_EXIT_SECONDS = 600
TURN_BUFFER_LIMIT = 40000


def log(msg):
    print(time.strftime('%Y-%m-%d %H:%M:%S'), msg, flush=True)


class Client:
    def __init__(self, sock):
        self.sock = sock
        self.lock = threading.Lock()
        self.subscribed = False
        self.alive = True

    def send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                self.alive = False


class Session:
    def __init__(self, sid, proc, info):
        self.sid, self.proc, self.info = sid, proc, info
        self.started_at = time.time()
        self.sticky = {}
        self.turn = []
        self.pending = set()
        self.first_response_seen = False
        self.stderr = []
        self.write_lock = threading.Lock()
        self.ended = False


class Holder:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.sessions = {}
        self.clients = []
        # One lock for recording, broadcasting and subscription snapshots, so replay and live
        # output can never interleave or duplicate.
        self.lock = threading.RLock()
        self.last_activity = time.time()

    def broadcast(self, obj):
        for c in [c for c in self.clients if c.subscribed and c.alive]:
            c.send(obj)

    # ---- child processes
    def start(self, client, msg):
        sid = str(msg.get('sid') or '')
        with self.lock:
            s = self.sessions.get(sid)
            if s and not s.ended:
                return client.send({'event': 'started', 'sid': sid, 'pid': s.proc.pid, 'existing': True})
        try:
            proc = subprocess.Popen(msg['args'], cwd=msg['cwd'], env=msg['env'], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:  # noqa: BLE001 - reported to the client
            return client.send({'event': 'error', 'sid': sid, 'message': 'could not start: %s' % e})
        s = Session(sid, proc, msg.get('info') or {})
        with self.lock:
            self.sessions[sid] = s
            client.send({'event': 'started', 'sid': sid, 'pid': proc.pid})
            self.broadcast({'event': 'session', 'sid': sid, 'pid': proc.pid, 'started_at': s.started_at, 'info': s.info})
        threading.Thread(target=self.read_stdout, args=(s,), daemon=True).start()
        threading.Thread(target=self.read_stderr, args=(s,), daemon=True).start()
        log('session started %s pid=%d' % (sid[:8], proc.pid))

    def record(self, s, line, t):
        try:
            d = json.loads(line)
        except ValueError:
            d = {}
        kind = d.get('type')
        if kind == 'result':
            s.turn = [(t, line)]
            s.pending.clear()
            return
        if kind == 'control_response':
            if not s.first_response_seen:      # the initialize response
                s.sticky['init_response'] = line
                s.first_response_seen = True
            return
        if kind == 'system' and d.get('subtype') == 'init':
            s.sticky['init'] = line
            return
        if kind == 'system' and d.get('subtype') == 'session_title_changed':
            s.sticky['title'] = line
            return
        if kind == 'rate_limit_event':
            s.sticky['rate_limit'] = line
            return
        if kind == 'control_request' and (d.get('request') or {}).get('subtype') == 'can_use_tool':
            s.pending.add(d.get('request_id'))
        elif kind == 'control_cancel_request':
            s.pending.discard(d.get('request_id'))
        s.turn.append((t, line))
        if len(s.turn) > TURN_BUFFER_LIMIT:
            del s.turn[:len(s.turn) - TURN_BUFFER_LIMIT]

    def read_stdout(self, s):
        try:
            for raw in s.proc.stdout:
                line = raw.decode('utf-8', 'replace').rstrip('\n')
                if not line:
                    continue
                t = time.time()
                with self.lock:
                    self.record(s, line, t)
                    self.broadcast({'event': 'output', 'sid': s.sid, 'line': line, 't': t})
        finally:
            try:
                s.proc.wait(30)
            except Exception:  # noqa: BLE001
                pass
            with self.lock:
                s.ended = True
                self.sessions.pop(s.sid, None)
                self.broadcast({'event': 'exited', 'sid': s.sid, 'code': s.proc.returncode, 'stderr': s.stderr[-20:]})
            self.last_activity = time.time()
            log('session exited %s code=%s' % (s.sid[:8], s.proc.returncode))

    def read_stderr(self, s):
        for raw in s.proc.stderr:
            text = raw.decode('utf-8', 'replace').strip()
            if text:
                s.stderr.append(text[:300])
                del s.stderr[:-40]

    def write(self, client, msg):
        sid = str(msg.get('sid') or '')
        s = self.sessions.get(sid)
        data = str(msg.get('data') or '')
        if not s or s.ended:
            return client.send({'event': 'error', 'sid': sid, 'message': 'session is not in the holder'})
        if '"control_response"' in data[:40]:
            try:
                rid = (json.loads(data).get('response') or {}).get('request_id')
                with self.lock:
                    s.pending.discard(rid)
            except ValueError:
                pass
        with s.write_lock:
            try:
                s.proc.stdin.write(data.encode('utf-8') + b'\n')
                s.proc.stdin.flush()
            except (OSError, ValueError):
                client.send({'event': 'error', 'sid': sid, 'message': 'session process is not responding'})

    def close(self, sid):
        s = self.sessions.get(sid)
        if not s:
            return
        log('releasing session %s' % sid[:8])
        try:
            s.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass

        def reap():
            try:
                s.proc.wait(20)
            except subprocess.TimeoutExpired:
                s.proc.terminate()
        threading.Thread(target=reap, daemon=True).start()

    def close_all(self):
        for sid in list(self.sessions):
            self.close(sid)

    def subscribe(self, client):
        with self.lock:
            for s in list(self.sessions.values()):
                if s.ended:
                    continue
                client.send({'event': 'session', 'sid': s.sid, 'pid': s.proc.pid, 'started_at': s.started_at, 'info': s.info})
                for key in ('init_response', 'init', 'title', 'rate_limit'):
                    if key in s.sticky:
                        client.send({'event': 'output', 'sid': s.sid, 'line': s.sticky[key], 't': s.started_at, 'replay': True})
                for t, line in s.turn:
                    if '"control_request"' in line[:40]:
                        try:
                            if json.loads(line).get('request_id') not in s.pending:
                                continue        # already answered: do not resurrect it
                        except ValueError:
                            continue
                    client.send({'event': 'output', 'sid': s.sid, 'line': line, 't': t, 'replay': True})
            client.send({'event': 'ready'})
            client.subscribed = True

    # ---- clients
    def serve_client(self, sock):
        client = Client(sock)
        with self.lock:
            self.clients.append(client)
        try:
            for raw in sock.makefile('rb'):
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self.last_activity = time.time()
                op = msg.get('op')
                if op == 'hello':
                    client.send({'event': 'hello', 'protocol': PROTOCOL, 'pid': os.getpid()})
                elif op == 'start':
                    self.start(client, msg)
                elif op == 'write':
                    self.write(client, msg)
                elif op == 'close':
                    self.close(str(msg.get('sid') or ''))
                elif op == 'subscribe':
                    self.subscribe(client)
                elif op == 'close_all':
                    self.close_all()
                    client.send({'event': 'ok'})
                elif op == 'shutdown':
                    log('shutdown requested: releasing sessions, holder exiting')
                    self.close_all()
                    client.send({'event': 'ok'})
                    threading.Thread(target=self.exit_soon, daemon=True).start()
        except OSError:
            pass
        finally:
            client.alive = False
            with self.lock:
                if client in self.clients:
                    self.clients.remove(client)
            try:
                sock.close()
            except OSError:
                pass
            self.last_activity = time.time()

    def exit_soon(self):
        time.sleep(1.5)
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        os._exit(0)

    def watchdog(self):
        while True:
            time.sleep(30)
            if not self.sessions and not self.clients and time.time() - self.last_activity > IDLE_EXIT_SECONDS:
                log('idle; holder exiting')
                try:
                    os.unlink(self.socket_path)
                except OSError:
                    pass
                os._exit(0)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--socket' not in argv:
        sys.exit('usage: python -m kovan.holder --socket PATH')
    socket_path = os.path.abspath(argv[argv.index('--socket') + 1])
    state_dir = os.path.dirname(socket_path)
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    os.chmod(state_dir, 0o700)
    if os.path.exists(socket_path):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(socket_path)
            probe.close()
            print('holder already running')
            return
        except OSError:
            os.unlink(socket_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)
    try:
        server.bind(socket_path)
    finally:
        os.umask(old_umask)
    os.chmod(socket_path, 0o600)
    server.listen(8)
    holder = Holder(socket_path)

    def on_term(*_):
        log('SIGTERM: releasing sessions, holder exiting')
        holder.close_all()
        time.sleep(1.5)
        try:
            os.unlink(socket_path)
        except OSError:
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
    threading.Thread(target=holder.watchdog, daemon=True).start()
    log('holder ready (protocol %d, pid %d)' % (PROTOCOL, os.getpid()))
    while True:
        conn, _ = server.accept()
        threading.Thread(target=holder.serve_client, args=(conn,), daemon=True).start()


if __name__ == '__main__':
    main()
