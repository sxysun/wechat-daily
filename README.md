# wechat-daily

Headless daily exporter for your **local** WeChat (微信) chat history on macOS.
Install once; it fetches your history every morning in the background and keeps
plain-text files + dated snapshots — no manual steps after setup.

It wraps [`wechat-cli`](https://www.npmjs.com/package/@canghe_ai/wechat-cli)
(which extracts the on-device SQLCipher keys) and adds scheduling, a **hybrid
incremental/full** strategy, snapshots, and self-healing.

> ⚠️ Reads **your own** WeChat data on **your own** Mac. The exports are plaintext
> chat logs — store and share them carefully.

## Requirements

- **macOS** (uses `launchd` for scheduling and reads WeChat's app container)
- **Python 3.8+** (standard library only — no pip dependencies)
- **[`wechat-cli`](https://www.npmjs.com/package/@canghe_ai/wechat-cli)** ≥ 0.2.4 — `npm i -g @canghe_ai/wechat-cli`
- WeChat for Mac, logged in
- *Optional, for Streams:* the [`router`](https://router.feedling.app) CLI (publishing) and
  [Claude Code](https://claude.com/claude-code) (`claude`, for agent-interpreted streams)
- *Optional, for RedNote streams:* [`OpenCLI`](https://github.com/jackwener/OpenCLI)
  with its Browser Bridge extension, Chrome logged into `rednote.com`, plus `router`
  and `claude`

## How the hybrid mode works

- **Incremental (daily default):** lists your sessions and, for each chat with
  new activity, fetches **only the new messages** (via `--start-time` from the
  last-seen minute, de-duplicated at the boundary) and appends them. Idle chats
  are skipped entirely. A hyperactive 350k-message group costs ~seconds/day, not
  a full re-export.
- **Full (weekly + self-heal):** re-exports every chat from scratch. Runs once a
  week (default Monday), or whenever the last full run is older than 7 days, or
  when there's no prior state. Catches edits, deletions, backfill, and any day a
  run was missed.

Either way it rewrites `all/<chat>.txt`, refreshes `MANIFEST.tsv` / `SUMMARY.md`,
and writes a dated `daily/YYYY-MM-DD.tar.gz` snapshot (last 14 kept).

## Install

```bash
npm install -g @canghe_ai/wechat-cli      # prerequisite
./install.sh                              # symlinks `wechat-daily` onto PATH
wechat-daily init                         # detect WeChat, extract keys, install agent
wechat-daily run --full                   # first full backfill
```

### One-time macOS permission (required for the headless run)
The scheduled job runs without a UI, so macOS won't show an "Allow" prompt — you
must pre-grant **Full Disk Access** (System Settings → Privacy & Security) to both:

- the `node` binary backing wechat-cli (e.g. `…/bin/node`)
- the `wechat-cli` binary itself

Without this, the background run can't read WeChat's protected data container.

## Commands

| Command | Description |
|---|---|
| `wechat-daily init` | One-time setup: detect WeChat, extract keys, install the daily agent |
| `wechat-daily run [--auto\|--full\|--incremental]` | Export now (`--auto` picks hybrid mode). Prints a per-chat line with how many new messages were added and of what type |
| `wechat-daily list [query] [--limit N]` | Chats by message count (filter by name) |
| `wechat-daily peek <chat> [--limit N]` | Print the most recent messages of a chat |
| `wechat-daily stats [chat]` | Message-type breakdown (text / image / link / sticker / …) overall or for one chat |
| `wechat-daily status` | Show keys, schedule, last run, next mode |
| `wechat-daily keys` | (Re)extract DB keys — needs sudo + WeChat open (GUI password prompt) |
| `wechat-daily install-agent` / `uninstall-agent` | Manage the LaunchAgent |
| `wechat-daily config [key value]` | View/set config |
| `wechat-daily uninstall` | Remove agent + app state (keeps your exports) |

### Seeing what's being exported

```
$ wechat-daily run --incremental
  [  1/579] ✓ Dev Community        group +580  text524 sticker31 img21 link2 file1 sys1
  [  5/579] ✓ Alex                 dm    +1    link1
Done (incremental): 6 updated, 566 idle skipped, 0 failed.
New messages added: 590  →  text532 sticker31 img22 link3 file1 sys1

$ wechat-daily stats "Dev Community"
  messages : 355,241
  range    : 2026-03-31 14:47  →  2026-06-25 13:50
  by type  :
    text     313893  88.4%  ███████████████████████████████████
    sticker   19604   5.5%  ██
    image     18396   5.2%  ██
```

## Streams — publish groups as queryable Router feeds

Promote a WeChat group into a **stream**: each daily run pushes that group's new
messages to [Router](https://router.feedling.app) (via the `router` CLI) so others
can search/follow them. Curation per stream:

- **`raw`** (default) — verbatim messages. `identity` (real / pseudonymize / drop),
  `redact` (phone/email/wechat_id), `chunk_size` apply.
- **`agent`** — instead of dumping messages, **local Claude Code** (`claude -p`) reads
  the batch and publishes an *interpreted daily intel update* (TL;DR, developments,
  signals/deltas, tools & links, sentiment). It's stateful (keeps a per-stream running
  brief at `~/.wechat-daily/streams/<id>.brief.md`) and can web-search to enrich.
  Configure `lens` (analyst objective), `agent_model` (sonnet/opus/haiku),
  `language`, and `source_note`. Output is one entry, so no chunking. Uses your
  existing Claude Code — no API keys.

Run **`wechat-daily streams`** (no args) to open the **terminal cockpit**: a two-pane
curses UI — chat library on the left, per-stream config + live preview on the right.
`↑↓` move · `space` promote / toggle field · `tab` switch panes · `←→` cycle options ·
`enter` edit text · `d` refresh preview · `p` publish · `q` quit. Or use subcommands:

| Command | Description |
|---|---|
| `wechat-daily streams` | Open the TUI cockpit |
| `wechat-daily streams add <chat>` | Promote a chat to a stream (raw by default) |
| `wechat-daily streams list` | List configured streams |
| `wechat-daily streams preview <id\|chat>` | Dry-run: show the exact entry that would publish |
| `wechat-daily streams publish <id> [--day]` | Publish new messages now (`--day` = latest full day) |
| `wechat-daily streams config <id> <key> <val>` | Set curation / identity / redact / channel / tags / chunk_size / status |
| `wechat-daily streams remove <id>` | Delete a stream config |

Each daily agent run publishes all `active` streams automatically (only their
new-since-last messages). Big days are split into chunked entries. Per-stream config
lives in `~/.wechat-daily/streams/<id>.json`.

## RedNote streams — daily market research via OpenCLI

You can also publish a daily RedNote search as a Router intel feed. This uses
OpenCLI's built-in `rednote` adapter, not third-party OpenCLI plugins.

Prereqs:

```bash
npm install -g @jackwener/opencli
# Install OpenCLI Browser Bridge, then keep Chrome logged into https://www.rednote.com
opencli rednote login --site-session persistent
wechat-daily rednote check
router --version
claude --version
```

Create and test a RedNote stream:

```bash
wechat-daily rednote add "AI hardware"
wechat-daily rednote config ai-hardware language 中文
wechat-daily rednote preview ai-hardware
wechat-daily rednote publish ai-hardware
```

Useful knobs:

```bash
wechat-daily rednote config ai-hardware limit 30          # search rows
wechat-daily rednote config ai-hardware detail_limit 8    # note pages to open
wechat-daily rednote config ai-hardware lens "consumer AI product demand and creator sentiment"
wechat-daily rednote config ai-hardware channel rednote-ai-hardware
wechat-daily rednote config ai-hardware status paused
```

Active RedNote streams run after the normal daily WeChat export. Config lives in
`~/.wechat-daily/rednote/<id>.json`; each stream keeps a running brief at
`~/.wechat-daily/rednote/<id>.brief.md` so the agent can call out deltas instead of
repeating yesterday's themes.

If your scheduled `launchd` environment cannot find a globally installed binary, set
`WECHAT_DAILY_OPENCLI=/path/to/opencli` or `WECHAT_DAILY_CLAUDE=/path/to/claude`.
The RedNote integration calls OpenCLI with `--site-session persistent`; if collection
ever reports `AUTH_REQUIRED`, rerun the `opencli rednote login --site-session persistent`
command above in the same Chrome profile.

## Config (`~/.wechat-daily/config.json`)

| Key | Default | Meaning |
|---|---|---|
| `out_dir` | `~/wechat_exports` | where exports + snapshots go |
| `hour` / `minute` | `10` / `0` | daily run time |
| `full_weekday` | `0` (Mon) | weekday for the weekly full run |
| `full_max_age_days` | `7` | force a full run if last full is older |
| `retain_snapshots` | `14` | dated snapshots to keep |

Change one with e.g. `wechat-daily config hour 8` then `wechat-daily install-agent`.

## When keys break
If WeChat rotates keys or adds a DB shard, exports start failing and you'll get a
"WeChat export degraded" notification. Fix with one command (sudo + WeChat open):

```bash
wechat-daily keys
```

## Layout

```
~/wechat-daily/          # the tool (this repo)
~/.wechat-daily/         # config.json, state.json, run.sh, streams/
<out_dir>/
  all/                   # one .txt per chat (latest)
  daily/                 # dated .tar.gz snapshots
  logs/                  # per-day run logs
  MANIFEST.tsv  SUMMARY.md
```

## Security & privacy

- Everything runs **locally**. Exports never leave your machine unless *you*
  configure a Stream and publish it.
- Key extraction needs `sudo` once (memory-scan of the running WeChat process) and
  Full Disk Access for the scheduled job. Keys are cached in `~/.wechat-cli/`.
- Streams are **publish-by-exception**: only groups you explicitly promote are
  published; DMs and un-promoted chats stay local. Per-stream `identity`
  (pseudonymize / drop senders) and `redact` (phone/email/wechat-id) let you strip
  PII; `agent` mode publishes an abstracted intel summary rather than raw messages.
- Publishing third-party group messages may carry consent/legal obligations
  depending on your jurisdiction — you are responsible for what you publish.

## License

MIT © sxysun

---

*Not affiliated with Tencent/WeChat. For exporting and managing **your own** chat
history on **your own** device.*
