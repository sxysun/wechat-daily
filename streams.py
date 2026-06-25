#!/usr/bin/env python3
"""
wechat-daily streams — promote exported WeChat groups into queryable Router feeds.

A *stream* is a per-chat publishing config. Each daily run (or `streams publish`)
takes that chat's NEW messages, applies the stream's curation/identity policy, and
pushes them to Router via the `router` CLI so others can search/follow them.

v1 default curation is RAW passthrough. `identity` / `redact` / `drop_system` are
optional knobs (off by default for raw).

Used by wechat_daily.py; not run directly.
"""
from __future__ import annotations
import glob, json, os, re, shutil, subprocess, tempfile
from collections import Counter
from datetime import datetime

import wechat_daily as wd

STREAMS_DIR = os.path.join(wd.APP_DIR, "streams")
DEFAULT_TAGS = ["stream", "wechat", "market-research"]

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
WXID_RE = re.compile(r"wxid_[A-Za-z0-9]+")
SENDER_RE = re.compile(r"^(\[[^\]]+\] )([^:]{1,40}?)(: )")

STREAM_DEFAULTS = {
    "curation": "raw",         # raw | anonymized | agent
    "identity": "real",        # real | pseudonymize | drop
    "redact": [],              # any of: phone, email, wechat_id
    "drop_system": False,
    "chunk_size": 1000,        # max messages per Router entry (raw/anonymized)
    "lens": "",                # agent mode: analyst objective for this stream
    "agent_model": "sonnet",   # agent mode: sonnet (cheap) | opus (best) | haiku
    "tags": DEFAULT_TAGS,
    "status": "active",
    "last_published_at": 0,
}

CLAUDE = shutil.which("claude") or "claude"

# ---------- store ----------

def _dir(): os.makedirs(STREAMS_DIR, exist_ok=True); return STREAMS_DIR
def _path(sid): return os.path.join(_dir(), f"{sid}.json")

def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "stream"

def load_all():
    out = []
    for p in sorted(glob.glob(os.path.join(_dir(), "*.json"))):
        try: out.append(json.load(open(p)))
        except Exception: pass
    return out

def load_one(sid):
    p = _path(sid)
    return json.load(open(p)) if os.path.exists(p) else None

def save_one(c):
    with open(_path(c["id"]), "w") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)

# ---------- message windowing ----------

def block_date(b):
    m = wd.TS_RE.match(b[0]); return m.group(1)[:10] if m else ""

def block_epoch(b):
    m = wd.TS_RE.match(b[0])
    if not m: return 0
    try: return int(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").timestamp())
    except Exception: return 0

def window(blocks, since_ts=None, latest_day=False):
    if latest_day:
        days = [d for d in (block_date(b) for b in blocks) if d]
        if not days: return []
        mx = max(days)
        return [b for b in blocks if block_date(b) == mx]
    if since_ts:
        return [b for b in blocks if block_epoch(b) > since_ts]
    return blocks

# ---------- curation ----------

def _redact(text, kinds, counter):
    if "phone" in kinds:
        text, n = PHONE_RE.subn("[phone]", text); counter["phone"] += n
    if "email" in kinds:
        text, n = EMAIL_RE.subn("[email]", text); counter["email"] += n
    if "wechat_id" in kinds:
        text, n = WXID_RE.subn("[wxid]", text); counter["wechat_id"] += n
    return text

def curate(stream, blocks):
    """Apply identity + redaction policy. Returns (lines:list[str], stats:dict)."""
    out, alias = [], {}
    stripped = Counter()
    for b in blocks:
        if stream.get("drop_system") and b[0].startswith("[") and "[系统]" in b[0]:
            continue
        text = wd.block_text(b)
        ident = stream.get("identity", "real")
        if ident in ("pseudonymize", "drop"):
            m = SENDER_RE.match(b[0])
            if m and m.group(2) != "me":
                if ident == "drop":
                    repl = m.group(1) + ":"
                else:
                    a = alias.setdefault(m.group(2), f"Member-{len(alias)+1:02d}")
                    repl = m.group(1) + a + ": "
                text = repl + text[m.end():]
        if stream.get("redact"):
            text = _redact(text, stream["redact"], stripped)
        out.append(text)
    return out, {"aliased": len(alias), "stripped": dict(stripped)}

def header(stream, n, blocks):
    first = wd.block_min(blocks[0]) if blocks else ""
    last = wd.block_min(blocks[-1]) if blocks else ""
    return (f"# {stream['name']} · WeChat raw feed\n"
            f"> source: WeChat group · messages: {n} · {first} → {last} · "
            f"curation: {stream['curation']}/{stream['identity']}\n\n"
            f"```\n")

def build_entry(stream, lines, blocks):
    return header(stream, len(lines), blocks) + "\n".join(lines) + "\n```\n"

# ---------- publish ----------

def router_bin():
    return shutil.which("router") or "router"

def router_write(summary, body, channel, tags, oneliner, keywords):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body); tmp = f.name
    cmd = [router_bin(), "write", summary, "--file", tmp, "--tag", ",".join(tags[:5])]
    if channel: cmd += ["--channel", channel]
    if oneliner: cmd += ["--oneliner", oneliner[:15]]
    if keywords: cmd += ["--search-keywords", ",".join(keywords[:8])]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try: os.remove(tmp)
    except OSError: pass
    eid = ""
    m = re.search(r"\b([a-z0-9]{6,}-[a-z0-9]{4,})\b", r.stdout)
    if m: eid = m.group(1)
    return r.returncode == 0, eid, (r.stdout + r.stderr).strip()

def chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

# ---------- agent (local Claude Code as the analyst) ----------

def brief_path(stream):
    return os.path.join(_dir(), f"{stream['id']}.brief.md")

def run_agent(stream, blocks):
    """Interpret a batch of messages into a market-intel update via local Claude Code.
    Stateful: reads/writes a per-stream running brief. Returns (intel_md, ok)."""
    import tempfile
    tf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    for b in blocks:
        tf.write(wd.block_text(b) + "\n")
    tf.close()
    bp = brief_path(stream)
    brief = open(bp).read() if os.path.exists(bp) else ""
    lens = stream.get("lens") or ("general market intelligence — products, companies, "
                                  "deals, hiring, sentiment, notable links")
    model = stream.get("agent_model", "sonnet")
    prompt = (
        f"Read the WeChat group messages at {tf.name} (format: [time] sender: text).\n"
        "Produce a DAILY INTEL UPDATE for market-research subscribers of this stream.\n"
        "Sections: **TL;DR** (1-2 lines), **New developments** (bullets), "
        "**Signals & deltas**, **Tools & links** (keep URLs), **Sentiment**.\n"
        "Rules: pseudonymize all people (never real names); be signal-dense; drop pure "
        "social chatter; you may web-search to enrich an unfamiliar product/company in one line.\n"
        + (f"\nPrior running brief (use for continuity/deltas):\n{brief}\n" if brief else "")
        + "\nAfter the update, print a line containing exactly <<<BRIEF>>> then a concise "
        "updated running brief (key entities, ongoing threads, sentiment trend) for the next run.\n"
        "Output clean markdown only, no preamble."
    )
    sysp = ("You are a sharp market-intelligence analyst turning noisy community chat into "
            f"dense, queryable signal for a private stream. Lens: {lens}.")
    try:
        r = subprocess.run([CLAUDE, "-p", prompt, "--model", model,
                            "--allowedTools", "Read", "WebSearch",
                            "--append-system-prompt", sysp],
                           capture_output=True, text=True, timeout=900,
                           stdin=subprocess.DEVNULL)
    finally:
        try: os.unlink(tf.name)
        except OSError: pass
    out = (r.stdout or "").strip()
    # Guard: never publish a limit/error/empty string as if it were intel.
    low = out.lower()
    if (not out or len(out) < 200
            or any(p in low for p in ("session limit", "hit your", "usage limit",
                                      "invalid api key", "rate limit"))
            or out.startswith("Execution error")):
        return "", False
    intel, _, newbrief = out.partition("<<<BRIEF>>>")
    if newbrief.strip():
        with open(bp, "w") as f:
            f.write(newbrief.strip() + "\n")
    return intel.strip(), True

# ---------- resolve a chat / ephemeral stream ----------

def resolve(cfg, query):
    """Return (filename, displayname) for a query, or (None, list_of_matches)."""
    files = wd.find_files(cfg, query)
    if not files: return None, []
    if len(files) > 1: return None, files
    return files[0], wd.display_name(files[0])

def build_default(fn, name):
    """Build a default stream config from an export filename + display name."""
    sid = slugify(name)
    if sid in ("", "stream"):  # non-ASCII names slug to empty → fall back to the username
        user = fn[:-4].split("__")[-1] if fn.endswith(".txt") else fn
        sid = slugify(user) or "stream"
    s = dict(STREAM_DEFAULTS)
    s.update({"id": sid, "name": name, "source_file": fn, "channel": "wechat-" + sid})
    return s

def ephemeral(cfg, query):
    fn, name = resolve(cfg, query)
    if not fn: return None, name
    return build_default(fn, name), name

# ---------- commands ----------

def cmd_add(cfg, query):
    s, name = ephemeral(cfg, query)
    if not s:
        print(f"No single chat matches '{query}'."); _print_matches(cfg, name); return 1
    if load_one(s["id"]):
        print(f"Stream '{s['id']}' already exists. Use: wechat-daily streams config {s['id']} <key> <val>"); return 1
    save_one(s)
    print(f"✓ stream '{s['id']}' created  ({name})")
    print(f"  curation={s['curation']} identity={s['identity']} channel={s['channel']} status={s['status']}")
    print(f"  preview:  wechat-daily streams preview {s['id']}")
    return 0

def _print_matches(cfg, matches):
    if isinstance(matches, list) and matches:
        print("Matches:")
        for fn in matches[:20]: print(f"  • {wd.display_name(fn)}")

def cmd_list(cfg):
    streams = load_all()
    if not streams:
        print("No streams yet.  Add one:  wechat-daily streams add <chat>"); return 0
    print(f"{'id':28} {'curation':12} {'identity':12} {'status':8} channel")
    for s in streams:
        when = datetime.fromtimestamp(s["last_published_at"]).strftime("%m-%d %H:%M") if s.get("last_published_at") else "never"
        print(f"{s['id'][:28]:28} {s['curation']:12} {s['identity']:12} {s['status']:8} {s.get('channel','')}  (last: {when})")
    return 0

def cmd_config(cfg, sid, key, val):
    s = load_one(sid)
    if not s: print(f"No stream '{sid}'."); return 1
    if not key:
        print(json.dumps(s, indent=2, ensure_ascii=False)); return 0
    if val is None: print(f"{key} = {s.get(key)}"); return 0
    if key in ("tags", "redact"): v = [x for x in re.split(r"[,\s]+", val) if x]
    elif key == "chunk_size": v = int(val)
    elif key in ("drop_system",): v = val.lower() in ("1", "true", "yes", "on")
    else: v = val
    s[key] = v; save_one(s); print(f"{sid}.{key} = {v}"); return 0

def cmd_remove(cfg, sid):
    p = _path(sid)
    if os.path.exists(p): os.remove(p); print(f"removed stream '{sid}'")
    else: print(f"no stream '{sid}'")
    return 0

def _resolve_stream(cfg, ref):
    """ref may be a stream id or a chat query."""
    s = load_one(ref)
    if s: return s, wd.display_name(s.get("source_file", "")) or s["name"]
    return ephemeral(cfg, ref)

def cmd_preview(cfg, ref):
    s, name = _resolve_stream(cfg, ref)
    if not s: print(f"No chat/stream matches '{ref}'."); _print_matches(cfg, name); return 1
    path = os.path.join(cfg["out_dir"], "all", s["source_file"])
    _, blocks = wd.read_blocks(path)
    win = window(blocks, latest_day=True)
    if s.get("curation") == "agent":
        print(f"── AGENT PREVIEW: {s['id']} ───────────────────────────")
        print(f"lens: {s.get('lens') or '(default market intelligence)'}  ·  model: {s.get('agent_model','sonnet')}")
        print(f"analyzing {len(win)} messages via local Claude Code… (~1 min)\n")
        intel, ok = run_agent(s, win)
        print(intel if ok else "(agent produced no output)")
        print("\n───────────────────────────────────────────────────────")
        print(f"Publish for real:  wechat-daily streams publish {s['id']} --day")
        return 0
    lines, stats = curate(s, win)
    entry = build_entry(s, lines, win)
    print(f"── PREVIEW (latest day) ───────────────────────────────")
    print(f"stream: {s['id']}  ·  channel: {s.get('channel')}  ·  curation: {s['curation']}/{s['identity']}")
    print(f"would publish: {len(lines)} messages"
          + (f"  ·  🔒 aliased {stats['aliased']} senders" if stats["aliased"] else "")
          + (f"  ·  stripped {stats['stripped']}" if stats["stripped"] else ""))
    cs = s.get("chunk_size", 1000)
    if len(lines) > cs:
        print(f"⚠ would split into {-(-len(lines)//cs)} Router entries (chunk_size={cs})")
    print("───────────────────────────────────────────────────────")
    body_lines = entry.split("\n")
    if len(body_lines) > 60:
        print("\n".join(body_lines[:45]))
        print(f"\n   … [{len(body_lines)-55} lines omitted] …\n")
        print("\n".join(body_lines[-10:]))
    else:
        print(entry)
    print("───────────────────────────────────────────────────────")
    print(f"Publish for real:  wechat-daily streams publish {s['id']}")
    return 0

def publish_stream(cfg, s, verbose=True, force_day=False):
    path = os.path.join(cfg["out_dir"], "all", s["source_file"])
    if not os.path.exists(path):
        if verbose: print(f"  {s['id']}: source file missing"); return 0
    _, blocks = wd.read_blocks(path)
    since = int(s.get("last_published_at", 0) or 0)
    # First publish (never published) bootstraps with the latest DAY, not all history —
    # otherwise since=0 would dump the entire backlog (e.g. 355k msgs → 355 entries).
    if force_day or since <= 0:
        win = window(blocks, latest_day=True)
    else:
        win = window(blocks, since_ts=since)
    if not win:
        if verbose: print(f"  {s['id']}: nothing new"); return 0
    stamp0 = datetime.now().strftime("%Y-%m-%d")
    # ----- agent mode: publish ONE interpreted intel update, not raw chunks -----
    if s.get("curation") == "agent":
        if verbose: print(f"  {s['id']}: analyzing {len(win)} msgs via claude ({s.get('agent_model','sonnet')})…")
        intel, ok = run_agent(s, win)
        if not ok or not intel:
            if verbose: print(f"  ✗ {s['id']}: agent produced nothing"); return 0
        summary = f"{s['name']} · {stamp0} · intel"
        oneliner = (s["name"][:9] + " " + stamp0[5:])[:15]
        ok2, eid, msg = router_write(summary, intel, s.get("channel"),
                                     s.get("tags", DEFAULT_TAGS), oneliner, [])
        if ok2:
            s["last_published_at"] = max(block_epoch(b) for b in win); save_one(s)
            if verbose: print(f"  ✓ {s['id']}: intel update → {s.get('channel')} [{eid}]")
            return 1
        if verbose: print(f"  ✗ {s['id']}: publish failed — {msg[:120]}")
        return 0
    cs = s.get("chunk_size", 1000)
    parts = list(chunk(win, cs)); total = len(parts); published = 0
    stamp = datetime.now().strftime("%Y-%m-%d")
    for i, part in enumerate(parts, 1):
        lines, stats = curate(s, part)
        if not lines: continue
        body = build_entry(s, lines, part)
        suffix = f" ({i}/{total})" if total > 1 else ""
        summary = f"{s['name']} · {stamp}{suffix} · {len(lines)} msgs"
        oneliner = (s["name"][:10] + " " + stamp[5:])[:15]
        ok, eid, msg = router_write(summary, body, s.get("channel"), s.get("tags", DEFAULT_TAGS), oneliner, [])
        if ok:
            published += 1
            if verbose: print(f"  ✓ {s['id']}{suffix}: {len(lines)} msgs → {s.get('channel')} [{eid}]")
        else:
            if verbose: print(f"  ✗ {s['id']}{suffix}: publish failed — {msg[:120]}")
            break
    if published:
        s["last_published_at"] = max(block_epoch(b) for b in win)
        save_one(s)
    return published

def cmd_publish(cfg, ref, dry=False, force_day=False):
    s, name = _resolve_stream(cfg, ref)
    if not s: print(f"No chat/stream matches '{ref}'."); return 1
    if not load_one(s["id"]):
        print(f"(‘{s['id']}’ isn’t saved yet — `streams add` it first, or this is a one-off)")
    if dry:
        return cmd_preview(cfg, ref)
    n = publish_stream(cfg, s, verbose=True, force_day=force_day)
    print(f"Published {n} entr{'y' if n==1 else 'ies'} for '{s['id']}'.")
    return 0

def publish_due(cfg, verbose=True):
    """Called by the daily agent after the export."""
    streams = [s for s in load_all() if s.get("status") == "active"]
    if not streams: return
    if verbose: print(f"\nstreams: publishing {len(streams)} active …")
    for s in streams:
        try: publish_stream(cfg, s, verbose=verbose)
        except Exception as e:
            if verbose: print(f"  ✗ {s.get('id')}: {e}")

# ---------- dispatch ----------

def dispatch(cfg, args):
    a = args.action or "ui"
    rest = list(args.rest)
    if a == "ui":
        import sys
        if not sys.stdout.isatty():
            return cmd_list(cfg)
        import tui
        return tui.run(cfg)
    if a in ("list", "status"): return cmd_list(cfg)
    if a == "add":     return cmd_add(cfg, " ".join(rest))
    if a == "remove":  return cmd_remove(cfg, rest[0] if rest else "")
    if a == "preview": return cmd_preview(cfg, " ".join(rest))
    if a == "publish": return cmd_publish(cfg, " ".join(r for r in rest), dry=args.dry, force_day=args.day)
    if a == "config":
        sid = rest[0] if rest else ""
        key = rest[1] if len(rest) > 1 else None
        val = " ".join(rest[2:]) if len(rest) > 2 else None
        return cmd_config(cfg, sid, key, val)
    print(f"unknown streams action '{a}'"); return 1
