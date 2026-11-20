"""Pull requests and the project's real CI for crews.

With "pr" a crew works on its own branch (kovan/<name>) and, when it finishes, opens a pull request on GitHub
(through the gh CLI) or a merge request on GitLab (through git push options — no extra tool needed).

With "ci" every milestone commit is pushed and the crew waits for the project's real CI on that commit:
GitHub Actions check runs and commit statuses (Jenkins and other services post those) through gh; GitLab
pipelines and commit statuses through glab; or, anywhere, a status command of your own. Red sends the milestone
back to the Developer with the failing jobs and the tail of their logs; green lets it count as done.

The status command gets the commit as {sha} (and the branch as {branch}) and answers with its exit code:
0 = green, 75 = still running, anything else = red (its output is shown to the Developer).
"""
import json
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit

from . import server

RED = ('failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure', 'error', 'failed', 'canceled')
PENDING_EXIT = 75


def run(cwd, args, timeout=60, env=None):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout or '', r.stderr or ''
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def remote_url(directory):
    code, out, _ = run(directory, ['git', 'remote', 'get-url', 'origin'])
    return out.strip() if code == 0 else ''


def forge_of(url):
    """'github' or 'gitlab' from a remote URL (https or ssh); GitLab is assumed for other hosts."""
    if not url:
        return None
    host = urlsplit(url).hostname if '://' in url else (url.split('@', 1)[-1].split(':', 1)[0])
    return 'github' if (host or '').lower() in ('github.com', 'www.github.com') else 'gitlab'


def tool_env():
    return {k: v for k, v in server.session_env().items() if not k.startswith(('CLAUDE', 'MCP_'))}


# ============================================================================ status
class Status:
    def __init__(self, state, summary='', details=''):
        self.state, self.summary, self.details = state, summary, details    # green | red | pending | none

    def __repr__(self):
        return 'Status(%s, %s)' % (self.state, self.summary)


def github_status(directory, sha, env=None):
    env = env or tool_env()
    code, out, err = run(directory, ['gh', 'api', 'repos/{owner}/{repo}/commits/%s/check-runs?per_page=100' % sha], env=env)
    if code != 0:
        return Status('pending', 'gh api failed: %s' % server.clip(err.strip() or out.strip(), 200))
    runs = (json.loads(out or '{}').get('check_runs') or [])
    code, out, _ = run(directory, ['gh', 'api', 'repos/{owner}/{repo}/commits/%s/status' % sha], env=env)
    statuses = (json.loads(out or '{}').get('statuses') or []) if code == 0 else []
    jobs = [(r.get('name'), r.get('status'), r.get('conclusion'), r) for r in runs] + \
        [(s.get('context'), 'completed' if s.get('state') != 'pending' else 'in_progress',
          {'success': 'success', 'pending': None}.get(s.get('state'), 'failure'), s) for s in statuses]
    if not jobs:
        return Status('none')
    failed = [j for j in jobs if j[2] in RED]
    running = [j for j in jobs if j[1] != 'completed']
    names = lambda js: ', '.join(sorted({str(j[0]) for j in js}))      # noqa: E731
    if failed and not running:
        return Status('red', 'failed: %s' % names(failed), github_details(directory, failed, env))
    if running:
        return Status('pending', 'running: %s' % names(running))
    return Status('green', 'passed: %s' % names(jobs))


def github_details(directory, failed, env):
    parts = []
    for name, _, _, raw in failed[:3]:
        part = '## %s (%s)\n' % (name, raw.get('html_url') or raw.get('target_url') or '')
        if raw.get('description'):                      # a commit status (Jenkins, …)
            part += raw['description'] + '\n'
        out = (raw.get('output') or {})
        if out.get('title') or out.get('summary'):
            part += '%s\n%s\n' % (out.get('title') or '', server.clip(out.get('summary') or '', 1500))
        if raw.get('id') and 'check_suite' in raw:      # a check run: annotations, then the failed steps' log
            code, ann, _ = run(directory, ['gh', 'api', 'repos/{owner}/{repo}/check-runs/%s/annotations' % raw['id']], env=env)
            if code == 0:
                anns = [a for a in json.loads(ann or '[]') or [] if a.get('annotation_level') in ('failure', 'warning', None)]
                for a in sorted(anns, key=lambda a: a.get('annotation_level') != 'failure')[:10]:
                    part += '- %s:%s %s\n' % (a.get('path'), a.get('start_line'), server.clip(a.get('message') or '', 300))
            m = re.search(r'/job/(\d+)', raw.get('html_url') or '')
            if m:
                code, log, _ = run(directory, ['gh', 'run', 'view', '--job', m.group(1), '--log-failed'], timeout=120, env=env)
                if code == 0 and log.strip():
                    part += '```\n%s\n```\n' % log[-3500:]
        parts.append(part)
    return server.mask_secrets('\n'.join(parts))[0]


def gitlab_status(directory, sha, env=None):
    env = env or tool_env()
    code, out, err = run(directory, ['glab', 'api', 'projects/:id/pipelines?sha=%s' % sha], env=env)
    if code != 0:
        return Status('pending', 'glab api failed: %s' % server.clip(err.strip() or out.strip(), 200))
    pipelines = json.loads(out or '[]') or []
    code, out, _ = run(directory, ['glab', 'api', 'projects/:id/repository/commits/%s/statuses' % sha], env=env)
    statuses = [s for s in (json.loads(out or '[]') if code == 0 else []) if s.get('name') != 'pipeline']
    if not pipelines and not statuses:
        return Status('none')
    states = [p.get('status') for p in pipelines[:1]] + [s.get('status') for s in statuses]
    if any(s in ('running', 'pending', 'created', 'waiting_for_resource', 'preparing', 'scheduled') for s in states):
        return Status('pending', 'running')
    if any(s in RED for s in states):
        details = []
        if pipelines and pipelines[0].get('status') in RED:
            pid = pipelines[0]['id']
            code, out, _ = run(directory, ['glab', 'api', 'projects/:id/pipelines/%s/jobs?scope[]=failed' % pid], env=env)
            for job in (json.loads(out or '[]') if code == 0 else [])[:3]:
                code, trace, _ = run(directory, ['glab', 'api', 'projects/:id/jobs/%s/trace' % job['id']], timeout=120, env=env)
                details.append('## %s (%s)\n```\n%s\n```' % (job.get('name'), job.get('web_url') or '', trace[-3500:]))
        for s in statuses:
            if s.get('status') in RED:
                details.append('## %s (%s)\n%s' % (s.get('name'), s.get('target_url') or '', s.get('description') or ''))
        return Status('red', 'failed', server.mask_secrets('\n'.join(details))[0])
    return Status('green', 'passed')


def command_status(directory, command, sha, branch, timeout=300):
    cmd = command.replace('{sha}', sha).replace('{branch}', branch or '')
    try:
        r = subprocess.run(cmd, shell=True, cwd=directory, capture_output=True, text=True, timeout=timeout,  # noqa: S602
                           env=tool_env())
    except subprocess.TimeoutExpired:
        return Status('pending', 'status command timed out')
    output = server.mask_secrets(server.head_tail((r.stdout or '') + (r.stderr or ''), 2000, 3000))[0]
    if r.returncode == 0:
        return Status('green', 'passed', output)
    if r.returncode == PENDING_EXIT:
        return Status('pending', 'running', output)
    return Status('red', 'exit %d' % r.returncode, output)


def status(directory, options, git_info, sha):
    if options.get('ci_command'):
        return command_status(directory, options['ci_command'], sha, git_info.get('branch'))
    if git_info.get('forge') == 'github':
        return github_status(directory, sha)
    return gitlab_status(directory, sha)


# ============================================================================ setup and pull requests
def prepare(directory, options, name):
    """Checks for "pr"/"ci" when a crew is created; returns the crew's git info. Raises KovanError."""
    if not (options.get('pr') or options.get('ci')):
        return None
    if not options.get('commit'):
        raise server.KovanError('pull requests and the CI gate need “Commit each milestone”')
    url = remote_url(directory)
    if not url:
        raise server.KovanError('pull requests and the CI gate need a git repository with an “origin” remote')
    if run(directory, ['git', 'rev-parse', '--verify', '-q', 'HEAD'])[0] != 0:
        raise server.KovanError('pull requests need a repository with at least one commit to branch from')
    forge = forge_of(url)
    if forge == 'github' and not shutil.which('gh', path=tool_env().get('PATH')):
        raise server.KovanError('install the GitHub CLI (gh) and run “gh auth login” for pull requests and the CI gate')
    if options.get('ci') and forge == 'gitlab' and not options.get('ci_command') and \
            not shutil.which('glab', path=tool_env().get('PATH')):
        raise server.KovanError('for the CI gate on GitLab install glab (and run “glab auth login”), or give a status command')
    base = run(directory, ['git', 'branch', '--show-current'])[1].strip() or 'main'
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40] or 'crew'
    branch, n = 'kovan/' + slug, 2
    while run(directory, ['git', 'rev-parse', '--verify', '-q', 'refs/heads/' + branch])[0] == 0:
        branch, n = 'kovan/%s-%d' % (slug, n), n + 1
    if options.get('pr'):
        code, _, err = run(directory, ['git', 'checkout', '-q', '-b', branch])
        if code != 0:
            raise server.KovanError('could not create the branch %s: %s' % (branch, server.clip(err, 200)))
    else:
        branch = base
    return {'forge': forge, 'base': base, 'branch': branch, 'pr_url': None}


def push(directory, git_info):
    code, _, err = run(directory, ['git', 'push', '-q', '-u', 'origin', 'HEAD:refs/heads/' + git_info['branch']],
                       timeout=180, env=tool_env())
    return code == 0, server.clip(err.strip(), 300)


def pr_body(crew):
    plan = (crew.get('plan') or {}).get('milestones') or []
    lines = [crew.get('goal') or '', '', '### Milestones', '']
    for m in plan:
        mark = 'x' if m['status'] == 'done' else ' '
        lines.append('- [%s] %s%s' % (mark, m['title'], ' (%s)' % m['commit'] if m.get('commit') else ''))
    lines += ['', 'Built by a [kovan](https://github.com/halilibrahimd27/kovan) crew: every milestone passed the '
              'check command and the Reviewer%s.' % (' and the CI' if crew['options'].get('ci') else '')]
    return '\n'.join(lines)


def open_request(directory, crew, draft=False):
    """Open (or find) the pull request on GitHub, or push with GitLab's merge-request push options.

    GitLab reads push options only from a push that updates the branch, so a crew opens its merge request with
    the first push of its branch; later pushes update it. Returns (url or None, message, pushed).
    """
    g = crew['git']
    if g['forge'] == 'github':
        args = ['gh', 'pr', 'create', '--base', g['base'], '--head', g['branch'], '--title', crew['name'],
                '--body', pr_body(crew)]
        code, out, err = run(directory, args + (['--draft'] if draft else []), timeout=120, env=tool_env())
        if code != 0 and 'already exists' in (err + out):
            code, out, err = run(directory, ['gh', 'pr', 'view', g['branch'], '--json', 'url', '--jq', '.url'], env=tool_env())
        url = (re.findall(r'https://\S+/pull/\d+', out) or [None])[-1]
        return url, '' if url else server.clip(err.strip() or out.strip(), 300), False
    opts = ['-o', 'merge_request.create', '-o', 'merge_request.target=' + g['base'],
            '-o', 'merge_request.title=' + crew['name'], '-o', 'merge_request.remove_source_branch']
    code, out, err = run(directory, ['git', 'push', '-u', 'origin', 'HEAD:refs/heads/' + g['branch']] + opts,
                         timeout=180, env=tool_env())
    url = (re.findall(r'https?://\S+/-/merge_requests/\d+', out + err) or [None])[-1]
    return url, '' if code == 0 else server.clip((err or out).strip(), 300), code == 0


def mark_ready(directory, crew):
    g = crew['git']
    if g['forge'] == 'github' and g.get('pr_url'):
        run(directory, ['gh', 'pr', 'ready', g['branch']], timeout=60, env=tool_env())
        run(directory, ['gh', 'pr', 'edit', g['branch'], '--body', pr_body(crew)], timeout=60, env=tool_env())


def wait(directory, options, git_info, sha, still_wanted, poll=20, first_seen_limit=300, sleep=time.sleep):
    """Poll until the CI on ``sha`` is green or red, or gives up. ``still_wanted()`` stops it early."""
    started = time.time()
    while still_wanted():
        st = status(directory, options, git_info, sha)
        if st.state in ('green', 'red'):
            return st
        waited = time.time() - started
        if st.state == 'none' and waited > first_seen_limit:
            return Status('none', 'no CI result appeared for %s within %d minutes' % (sha[:8], first_seen_limit // 60))
        if waited > options.get('ci_timeout', 1800):
            return Status('none', 'the CI did not finish within %d minutes' % (options.get('ci_timeout', 1800) // 60))
        sleep(poll)
    return None
