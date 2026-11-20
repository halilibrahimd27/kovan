# Security

kovan can make an AI coding agent run commands on your machine, so it is built to be boring and local.
This page describes what it protects against, what it does not, and how to report a problem.

## Threat model

kovan assumes **one user on one machine**. It protects your sessions from:

- **Web pages you visit.** A malicious site must not be able to read your transcripts or send prompts to
  your sessions through your browser.
- **Other machines on the network.** Nothing is reachable from outside this computer.
- **Accidental disclosure.** Tool output often contains credentials; kovan should not put them on screen
  or in its own logs by default.

It does **not** try to protect you from other processes running as your own user. Those can already read
`~/.claude`, start `claude` themselves, and talk to the holder socket. That is the same trust boundary as
Claude Code itself.

## What kovan does

| Concern | Mitigation |
|---|---|
| Network exposure | Binds `127.0.0.1` only. |
| DNS rebinding | Every request must carry `Host: 127.0.0.1:<port>` or `localhost:<port>`; anything else gets `403`. |
| Cross-site reads | `/api/*` rejects requests whose `Sec-Fetch-Site` is not `same-origin`/`none`. No CORS headers are ever sent. |
| Cross-site writes (CSRF) | Every state-changing request is a `POST` that needs **all** of: an `Origin` equal to kovan's own address, `Sec-Fetch-Site: same-origin` (when the browser sends it), a JSON content type, and an `X-Kovan-Token` header. The token is random, regenerated on every start, kept in memory only and embedded in the page. |
| Injection into the page | The UI escapes every string before rendering (including Markdown), and the page is served with a strict Content-Security-Policy (`default-src 'none'`, no remote scripts, styles, fonts or frames). |
| Input validation | Session ids must be UUIDs, task ids `[A-Za-z0-9_-]`, models match a strict pattern, permission modes and efforts come from allow-lists, images are limited to png/jpeg/gif/webp ≤ 5 MB, request bodies to 25 MB. |
| Secrets in tool output | Tool output is never shown in overviews or the activity feed. It is loaded only when you expand a single tool call, and likely secrets are masked first (`password=…`, `token: …`, `api_key=…`, `Authorization:` headers, credentials in URLs, PEM private keys, and common token formats such as `ghp_…`, `glpat-…`, `sk-ant-…`, `AKIA…`, JWTs). Masking is best effort — treat the UI like a terminal. |
| Logs | `~/.local/state/kovan/kovan.log` records actions (session started, message sent with its *length*, permission answered…) but never message content. |
| Child processes | Sessions kovan starts do not inherit `CLAUDE*`, `VSCODE_*` or `ELECTRON_*` variables from the shell that started kovan (for example another session's messaging token). |
| Holder socket | `~/.local/state/kovan/holder-<port>.sock`, in a `0700` directory, mode `0600`. If that path would exceed the Unix socket length limit (a long `XDG_STATE_HOME`), kovan uses `$TMPDIR/kovan-<uid>/` instead and refuses to start unless that directory is owned by you, mode `0700` and not a symlink. |
| Permissions | Sessions kovan starts default to **ask before acting**. You can switch a session to auto-accept edits, plan mode or "never ask" from the UI. |

## Crews

A crew runs AI roles for a long time without you watching every step, so it gets its own rules:

| Concern | Mitigation |
|---|---|
| Permission prompts | Roles run in Claude Code's "accept edits" mode (Edit/Write inside the workspace need no prompt). Every other prompt is answered by the crew's **policy**: **Safe** (default) allows file tools only for paths inside the crew's folder (resolved, so `..` and symlinks cannot escape), web search for the Ideator and Planner, and shell commands only from a short list — test runners and linters (`pytest`, `python -m unittest`, `npm test`, `go test`, `cargo test`, `make test`…), read-only git, and `ls`/`cat`/`mkdir`-style commands with relative paths. Pipes, redirection, `$( )`, backgrounding, `rm`, `sudo`, network tools, package installs, `--force` and absolute or `..` paths are never auto-allowed. **Ask** sends every prompt to you; **Trusted** starts the roles with "never ask". |
| Escalation | Anything the policy does not allow waits for you: the crew shows *what exactly* is being asked (the command or path), on its card and in the boardroom, and you answer it in that role's session. Questions (`AskUserQuestion`) and plan approvals always go to you. |
| The check command | Runs in the crew's folder with a timeout, without Claude Code's or kovan's environment variables, and its output is masked before it reaches the Reviewer or the page. A check command **the Planner proposes** runs only after you approve the plan — or, when plan approval is off, only if it is on the safe list above. |
| Commits | kovan stages with `git add -A` and then takes secret-looking files (`.env*`, `*.pem`, `*.key`, SSH keys, `credentials.json`, `secrets.*`, `.npmrc`, `.pypirc`, `.netrc`) back out of the index, and says so in the boardroom. A crew only starts in an empty folder or a **clean** git repository, so its commits contain nothing but its own work. Pushing is off unless you turn it on. |
| Workspace | A crew refuses `/`, your home directory and its parents, and a folder another active crew is using. |
| Usage | The finance officer pauses a crew when the 5-hour usage window reaches your threshold (90 % by default). |

**What the safe policy is not:** a sandbox. Test runners execute code the Developer wrote — `pytest` loads
`conftest.py`, `npm test` runs `package.json` scripts, `make test` runs the Makefile. The policy stops
one-off destructive or exfiltrating commands, not a role that deliberately writes harmful code into the
project and then runs its tests. Give crews goals you would give a junior colleague with access to that
folder; for anything you would not, run kovan inside a container or VM.

## The swarm engine

`--swarm URL` makes the kovan **server** (not the page) call the swarm engine's API. Only plain
`http://127.0.0.1` / `localhost` URLs are accepted, so the token (`KOVAN_SWARM_TOKEN`, or
`AIDEVSWARM_API_TOKEN`) is never sent to another host and kovan cannot be used as a proxy. kovan sends only
non-destructive commands (`inject_note`, `approve`, `pause_project`, `resume_project`); aborting a project
stays in the swarm's own UI and Telegram bot, where it asks for confirmation. Boardroom text from the swarm is
masked and escaped like any other content.

## Known limitations

- The `X-Kovan-Token` is embedded in the page, so any local process that can fetch
  `http://127.0.0.1:<port>/` can obtain it. That is within the "same user" boundary above.
- Secret masking uses patterns and will miss secrets that do not look like secrets.
- kovan reads files that Claude Code does not document (`~/.claude/sessions/*.json`, the transcript
  format). If those change, kovan may show wrong information until it is updated; it does not write to them.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (the *Security* tab → *Report a vulnerability*)
instead of a public issue. Include the kovan version (`kovan --version`), your OS, and steps to reproduce.
