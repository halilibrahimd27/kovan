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
| What gets committed | kovan records the git tree the Reviewer read. Before an extra role reads the diff and again right before the commit it stages the workspace and compares: if anything changed after the review (a role "fixing one more thing"), the check and the review run again, and after three rounds the crew stops for you. |
| Pull requests and CI | Pushing and opening requests use your own `git`, `gh` and `glab` credentials — kovan stores no forge tokens. A crew pushes only its own branch (`kovan/<name>`, or the current branch when only the CI gate is on), never with `--force`. The CI status command runs like the check command: in the crew's folder, with a timeout, without Claude Code's or kovan's variables, its output masked before the Developer sees it. CI logs are masked the same way. |
| Extra roles | Your own roles are prompts you write; they get the same permission policy as the built-in roles (no web access). A role that reads the diff *before the commit* is told not to edit files — and if it does, the rule above catches it. The Security auditor reads the code; it does not run scanners or anything that needs the network. It is a reviewer, not a guarantee. |
| Secret files | Every role session starts with Claude Code deny rules for `.env`, `.env.local`, `*.pem`, `*.key`, SSH keys, `.netrc` and `.pypirc` (`--disallowedTools`), so no role reads them even in "never ask" mode. A shell command that names a secret-looking file is never auto-allowed, and neither is a file tool on one. |
| Your command patterns | *Allowed commands* add to the safe list for one crew. A pattern matches word by word: `*` is a glob inside a word, `**` stands for any arguments. Words that `**` swallows may not be URLs, so a URL has to be written in the pattern (`curl ** http://127.0.0.1:18080* **` cannot also reach another host). Pipes, redirection, `$( )`, backgrounding, `rm`, `sudo`, `ssh`, `scp`, `chmod`, `chown`, `--force`, `--privileged` and absolute or `..` paths still never pass. *Denied commands* always go to you. |
| Push command | Runs your own command (`{branch}` filled in, shell-quoted) in the crew's folder, without Claude Code's or kovan's variables; its output is masked. Roles never run it, kovan does, after a milestone is reviewed and committed. kovan opens no request with it; at the end it shows a link that opens the request form. |
| Advisor | The advisor session answers in a fork (`claude -p --resume <id> --fork-session`) with only Read, Grep and Glob and the same secret-file rules. Its own transcript is not changed. kovan sends it the goal and the plan, once per crew, and shows the answer in the boardroom; the decision stays yours. |
| Usage | The finance officer pauses a crew when the 5-hour usage window reaches your threshold (90 % by default) or the 7-day window reaches its own (95 %). Working hours stop new turns outside the hours you set. |

**What the safe policy is not:** a sandbox. Test runners execute code the Developer wrote — `pytest` loads
`conftest.py`, `npm test` runs `package.json` scripts, `make test` runs the Makefile. The policy stops
one-off destructive or exfiltrating commands, not a role that deliberately writes harmful code into the
project and then runs its tests. Give crews goals you would give a junior colleague with access to that
folder; for anything you would not, run kovan inside a container or VM.

## Notifications

The webhook URL and the Telegram bot token are bearer secrets. kovan takes them from the config file or the
environment (`KOVAN_WEBHOOK_URL`, `KOVAN_TELEGRAM_TOKEN`), sends them only to the configured host (https only;
plain http only to `127.0.0.1`), and never writes them to its log — failures are logged as `HTTP 404` or
`ConnectionRefusedError`, without the URL.

| Concern | Mitigation |
|---|---|
| Someone else presses a button | Only callbacks and replies from the numeric Telegram user ids in `allow` are acted on; others are ignored (and logged without content). With an empty `allow` the bot only tells people their own id. |
| An old button | Every button carries a hash of the crew's state when the message was sent; once the crew has moved on (or you answered in kovan), the button says *no longer pending*, and kovan removes the buttons of outdated messages. |
| Approving commands from a phone | Off by default. With `"permissions": true` a permission prompt gets Allow / Deny buttons that answer exactly that prompt (its id must still be pending). |
| What leaves the machine | Crew names, plan and idea titles and summaries, blocked reasons, and the command a role wants to run — masked like everything kovan shows. Session content is not sent; `"sessions": true` sends only a session's name. |
| A stolen bot token | Lets the thief read what kovan sends and impersonate the bot, not press buttons as you (Telegram signs who pressed them). Revoke it in @BotFather. |

**Through Claude** (`"claude": true`) there is no secret to keep. Notifications travel with your own Claude Code
login, over Remote Control, and only your claude.ai account can open the **kovan** conversation. The relay session
has these limits:

- **Tools.** PushNotification is its only tool (`--tools PushNotification`). It loads no MCP servers and none of
  your claude.ai connectors. Its working folder is kovan's state folder, and kovan denies any other tool it asks
  for.
- **Environment.** It gets the same cleaned environment as kovan's sessions, without `KOVAN_*` variables such as
  the bot token.
- **Presence check.** It sets `CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK`, because nobody sits at its
  terminal. Without it, Claude would skip pushes it thinks you already see.
- **What it receives.** It gets the same masked text as the other channels. A plan summary is model output, so it
  could carry instructions. The relay is told never to follow them, and with a single tool the worst such text can
  do is word a push oddly.
- **Usage and local files.** Each message is one short model turn on your usage. A local transcript per relay
  start stays for two weeks, then kovan deletes it.

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
