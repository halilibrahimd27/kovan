# Changelog

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
- Sessions live in a small holder process, so restarting pano never interrupts them.
- `pano --demo` with synthetic data; zero dependencies (Python 3.9+ standard library).
