# Changelog

## 0.5.0 — 2026-11-25

Crews can now work on a real project for days without growing their context.

- **Models and effort per role**: for example Opus at high effort for the Planner and the Security auditor, Sonnet
  for the Tester. They can be changed while a crew runs; a change applies to the role's next session.
- **Fresh sessions every milestone** (on by default): the Developer, Tester, Reviewer and extra roles start each
  milestone with an empty context, and their task says what is already done. Earlier sessions stay listed with
  the crew.
- **Usage per role and milestone**: turns, output tokens, cache reads, context size and time, and how the weekly
  usage window moved. Crews also pause at the weekly window (95% by default), not only the 5-hour one.
- **Working hours and a start time**: a crew starts no new turn outside the days and hours you set, and goes on at
  the next start. A crew with a start time makes its branch when it starts. An **idle command** (e.g. `make down`)
  runs when it rests for the day or finishes.
- **Allowed and denied command patterns** per crew. `**` stands for any arguments, and URLs must be written in the
  pattern. The Developer is told which commands run without asking. Optional web access for every role.
- **Your own branch, push command and commit trailer.** At the end the crew gives you a link that opens the merge or
  pull request with the branches filled in. Commit subjects are kept to 72 characters.
- **Advisor**: one of your sessions reviews the plan once before you approve it, in a fork, so its own transcript is
  left untouched.
- **Settings → Edit** on the crew page changes these while the crew runs.
- Every role session starts with Claude Code deny rules for `.env`, keys and similar files. Commands that name such
  files are never auto-allowed.
- The boardroom table is larger: it can be switched between large, full and small. Seats show each role's model,
  effort and output tokens, and older exchanges fade.
- The Messages graph was redrawn: the busiest session sits in the middle, arrows are small, two-way lines run side
  by side and counts sit in labels.

## 0.4.0 — 2026-11-22

kovan can now notify you through Claude itself.

- **Notifications through Claude**: `"notify": {"claude": true}` sends a push from the Claude app on your phone,
  the same kind Claude Code sends itself, when a crew needs you or is done. You don't need a bot, a token or a
  webhook. kovan keeps one small Claude Code session connected with Remote Control. It shows up as **kovan** in
  the Claude app with every message and its details, and its only tool is PushNotification.
- The relay session starts with the first notification and rests after 20 quiet minutes. When it comes back it
  rejoins the same Claude conversation, also after kovan restarts. It uses a small model by default (`haiku`;
  `"model"` changes it), about 2,000 mostly cached tokens per notification.
- The crews page shows each channel's state. When a push did not go out it says why, for example that "Push when
  Claude decides" is off in `/config` or that Remote Control is not connected. `kovan --notify-test` covers the
  new channel too.

## 0.3.0 — 2026-11-21

Crews get extra roles, templates, a real CI gate and pull requests; kovan can reach you on your phone.

- **Extra crew roles**: up to four per crew, ready-made (Security auditor, Documenter, Performance reviewer) or
  your own — a name, its job, and whether it works after the Developer (may edit files) or reads the staged
  diff before the commit (blocks on the severities you choose). Your roles can be remembered for later crews.
- The **Security auditor** reviews every milestone's staged diff for injection, leaked secrets, authorisation and
  session flaws, risky dependencies and unsafe defaults; critical or high findings block the commit and go back to
  the Developer, lower ones are recorded in the commit message.
- **Crew templates**: Feature, Secure feature, Bug-fix and Security audit crews.
- Only reviewed work is committed: if the workspace changes after the Reviewer read the diff, kovan runs the check
  and the review again.
- The boardroom seats extra roles; milestones show each gate's findings; the New crew dialog has templates and a
  role editor (English and Turkish).
- README: a boardroom GIF (`scripts/make-gif.mjs`) and fresh screenshots; the package is ready for PyPI (absolute
  image links, metadata, a trusted-publishing workflow).
- **Pull requests and the CI gate**: a crew can work on its own branch and open a pull request (GitHub, `gh`) or a
  merge request (GitLab, push options), and wait for the project's real CI after every commit — GitHub check runs
  and commit statuses (Jenkins…), GitLab pipelines (`glab`), or your own status command. Red CI goes back to the
  Developer with the failing jobs, their annotations and log tails.
- **Notifications**: a Slack/Mattermost-compatible webhook and a Telegram bot tell you when a crew needs you or is
  done. Telegram buttons approve, revise or stop a plan, pick an idea, retry, skip or stop a blocked crew, and
  continue or pause after a milestone — only for the user ids you allow, never for an outdated message.
  Approving permission prompts from the phone is opt-in. `kovan --notify-test` checks the setup.

## 0.2.0 — 2026-11-20

kovan now lives in one repository with the swarm engine (formerly ai-dev-swarm) and gains crews and the
boardroom.

- **Crews**: a Planner, a Developer, an optional Tester and a Reviewer — real Claude Code sessions — build a
  goal milestone by milestone. Ideas mode (an Ideator proposes and scores ideas), plan approval and revision,
  your check command as the CI gate, review of the real staged diff, a commit per milestone, replanning
  (continue / amend / split / escalate), a retry budget, steering notes, pause / resume / stop / retry / skip,
  and a finance officer that pauses before the usage limit. Crews survive restarts.
- A permission policy for crews (safe / ask / trusted) that auto-answers only workspace file access, test
  runners and read-only git; secret-looking files are never staged; a Planner-proposed check command needs your
  approval unless it is on the safe list.
- **The boardroom**: one room per crew, one for the messages between your sessions, and one per swarm project;
  a round table with live seats and arcs for who spoke to whom, the meeting as a chat, action cards and a Speak
  box.
- `--swarm URL` (loopback only, token from `KOVAN_SWARM_TOKEN`) shows the swarm engine's boardroom and sends it
  notes, approvals, pauses and resumes.
- Crew role sessions are tagged in the sidebar; toasts when a crew needs you or finishes.
- Fix: the holder socket no longer fails to start when the state directory path is long (Unix sockets are
  limited to ~104 bytes); kovan falls back to a private directory under `$TMPDIR`.
- **Turkish UI** (EN/TR switch, `--lang`, `"lang"` in the config); crews created from a Turkish page get Turkish
  role prose and boardroom lines. User content is never translated.
- New sessions can start in any existing folder; it is added to the watched list and remembered.
- Notes: journals with `###` entries under bare `## <day>` headings (and mixed `##`/`###` files) are read
  correctly, newest first; undated files are shown as documents.
- Started from the Dock or launchd (a bare `/usr/bin:/bin` PATH), kovan takes the login shell's PATH so
  sessions find Homebrew, `~/.local/bin` and friends; `scripts/make-macos-app.sh` no longer leaks `PYTHONPATH`
  into sessions.
- The demo includes two crews and a swarm project.

## 0.1.0 — 2026-11-20

First public release.

- Watch every Claude Code session of one or more project directories in one live view: status,
  current tool call, last prompt and reply, context size, background tasks, subagents.
- See the messages sessions send each other, as a graph and a list.
- Drive sessions from the browser: new sessions, continue sessions you closed in VS Code, streaming
  replies, queued prompts, stop, images, slash commands, question and plan-approval cards, permission prompts.
- Tool call details on demand: commands and output, colored diffs with line numbers, likely secrets masked.
- Per-session controls: model, effort, permission mode, context meter with one-click compact, rename,
  rewind files to a prompt (with a preview), usage limits.
- ⌘K palette: jump to sessions and search every session and note.
- Sessions live in a small holder process, so restarting kovan never interrupts them.
- `kovan --demo` with synthetic data; zero dependencies (Python 3.9+ standard library).
