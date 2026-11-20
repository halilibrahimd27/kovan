# Security

pano can make an AI coding agent run commands on your machine, so it is built to be boring and local.
This page describes what it protects against, what it does not, and how to report a problem.

## Threat model

pano assumes **one user on one machine**. It protects your sessions from:

- **Web pages you visit.** A malicious site must not be able to read your transcripts or send prompts to
  your sessions through your browser.
- **Other machines on the network.** Nothing is reachable from outside this computer.
- **Accidental disclosure.** Tool output often contains credentials; pano should not put them on screen
  or in its own logs by default.

It does **not** try to protect you from other processes running as your own user. Those can already read
`~/.claude`, start `claude` themselves, and talk to the holder socket. That is the same trust boundary as
Claude Code itself.

## What pano does

| Concern | Mitigation |
|---|---|
| Network exposure | Binds `127.0.0.1` only. |
| DNS rebinding | Every request must carry `Host: 127.0.0.1:<port>` or `localhost:<port>`; anything else gets `403`. |
| Cross-site reads | `/api/*` rejects requests whose `Sec-Fetch-Site` is not `same-origin`/`none`. No CORS headers are ever sent. |
| Cross-site writes (CSRF) | Every state-changing request is a `POST` that needs **all** of: an `Origin` equal to pano's own address, `Sec-Fetch-Site: same-origin` (when the browser sends it), a JSON content type, and an `X-Pano-Token` header. The token is random, regenerated on every start, kept in memory only and embedded in the page. |
| Injection into the page | The UI escapes every string before rendering (including Markdown), and the page is served with a strict Content-Security-Policy (`default-src 'none'`, no remote scripts, styles, fonts or frames). |
| Input validation | Session ids must be UUIDs, task ids `[A-Za-z0-9_-]`, models match a strict pattern, permission modes and efforts come from allow-lists, images are limited to png/jpeg/gif/webp ≤ 5 MB, request bodies to 25 MB. |
| Secrets in tool output | Tool output is never shown in overviews or the activity feed. It is loaded only when you expand a single tool call, and likely secrets are masked first (`password=…`, `token: …`, `api_key=…`, `Authorization:` headers, credentials in URLs, PEM private keys, and common token formats such as `ghp_…`, `glpat-…`, `sk-ant-…`, `AKIA…`, JWTs). Masking is best effort — treat the UI like a terminal. |
| Logs | `~/.local/state/pano/pano.log` records actions (session started, message sent with its *length*, permission answered…) but never message content. |
| Child processes | Sessions pano starts do not inherit `CLAUDE*`, `VSCODE_*` or `ELECTRON_*` variables from the shell that started pano (for example another session's messaging token). |
| Holder socket | `~/.local/state/pano/holder-<port>.sock`, in a `0700` directory, mode `0600`. |
| Permissions | Sessions pano starts default to **ask before acting**. You can switch a session to auto-accept edits, plan mode or "never ask" from the UI. |

## Known limitations

- The `X-Pano-Token` is embedded in the page, so any local process that can fetch
  `http://127.0.0.1:<port>/` can obtain it. That is within the "same user" boundary above.
- Secret masking uses patterns and will miss secrets that do not look like secrets.
- pano reads files that Claude Code does not document (`~/.claude/sessions/*.json`, the transcript
  format). If those change, pano may show wrong information until it is updated; it does not write to them.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** (the *Security* tab → *Report a vulnerability*)
instead of a public issue. Include the pano version (`pano --version`), your OS, and steps to reproduce.
