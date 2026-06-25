#!/usr/bin/env python3
"""
wechat-daily — headless daily WeChat local-history exporter (macOS).

Install once; it fetches your WeChat history every day in the background using a
HYBRID strategy:

  • incremental (daily default): for each chat that received new messages, fetch
    ONLY the new messages (via --start-time from the last-seen minute) and append
    them — so even a 100k-message group costs ~seconds/day, not a full re-export.
  • full (weekly / self-heal): re-export every chat from scratch, catching edits,
    deletions, backfill, and any day a run was missed.

It wraps `wechat-cli` (which holds the decrypted SQLCipher keys), writes one
plain-text file per conversation, and keeps a dated compressed snapshot each day.

Visibility commands let you see what's in (and being added to) your exports:
  wechat-daily list                 # chats by message count
  wechat-daily peek  <chat>         # recent messages of a chat
  wechat-daily stats [chat]         # message-type breakdown (text/image/link/...)

Setup / ops:
  wechat-daily init | run [--auto|--full|--incremental] | status | keys
  wechat-daily install-agent | uninstall-agent | config [k v] | uninstall
"""
from __future__ import annotations
import argparse, glob, json, os, re, shutil, subprocess, sys, tarfile, time
from collections import Counter
from datetime import datetime

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, ".wechat-daily")
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATE_PATH = os.path.join(APP_DIR, "state.json")
AGENT_LABEL = "com.wechat-daily.export"
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", f"{AGENT_LABEL}.plist")
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)  # so `import streams`/`import tui` work via the symlink

DEFAULTS = {
    "out_dir": os.path.join(HOME, "wechat_exports"),
    "db_dir": "", "wechat_cli": "",
    "hour": 10, "minute": 0,
    "full_weekday": 0,        # Monday (datetime.weekday(): Mon=0 .. Sun=6)
    "full_max_age_days": 7,
    "retain_snapshots": 14,
    "export_limit": 2000000,
    "per_chat_timeout": 1800,
}

# message-type markers -> friendly label (order matters: first hit wins)
TYPE_MARKERS = [
    ("image", "[图片]"), ("link", "[链接]"), ("video", "[视频]"),
    ("sticker", "[动画表情]"), ("sticker", "[表情]"), ("file", "[文件]"),
    ("voice", "[语音]"), ("location", "[位置]"), ("transfer", "[转账]"),
    ("redpacket", "[红包]"), ("card", "[名片]"), ("music", "[音乐]"),
    ("system", "[系统]"),
]
TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]")

# ---------- json / config / state ----------

def _load(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception:
        return dict(default) if isinstance(default, dict) else default

def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def load_config():
    cfg = dict(DEFAULTS); cfg.update(_load(CONFIG_PATH, {}))
    cfg["wechat_cli"] = cfg.get("wechat_cli") or shutil.which("wechat-cli") or ""
    cfg["db_dir"] = cfg.get("db_dir") or detect_db_dir()
    return cfg

def load_state():
    return _load(STATE_PATH, {"last_run_at": 0, "last_full_at": 0, "chats": {}})

def detect_db_dir():
    base = os.path.join(HOME, "Library/Containers/com.tencent.xinWeChat/Data/"
                              "Documents/xwechat_files")
    cands = glob.glob(os.path.join(base, "wxid_*/db_storage"))
    return max(cands, key=os.path.getmtime) if cands else ""

# ---------- names / text ----------

def slug(s, n=40):
    s = re.sub(r"[/\\\n\r\t]", "_", s or "")
    return (re.sub(r"\s+", " ", s).strip()[:n] or "unknown")

def safe_user(u):
    return re.sub(r"[^0-9A-Za-z_.-]", "-", u or "unknown")

def chat_filename(s):
    typ = "group" if s.get("is_group") else "dm"
    return f"{typ}__{slug(s.get('chat') or s.get('username'))}__{safe_user(s.get('username'))}.txt"

def display_name(fname):
    parts = fname[:-4].split("__") if fname.endswith(".txt") else fname.split("__")
    return parts[1] if len(parts) >= 2 else fname

def notify(title, msg):
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "{title}"'],
                       timeout=10, capture_output=True)
    except Exception:
        pass

def log(out_dir, msg):
    logs = os.path.join(out_dir, "logs"); os.makedirs(logs, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with open(os.path.join(logs, f"{datetime.now():%Y-%m-%d}.log"), "a") as f:
        f.write(line + "\n")

# ---------- txt parsing ----------

def split_header_body(content):
    lines = content.split("\n")
    sep = next((i for i, l in enumerate(lines)
                if l.strip() and set(l.strip()) == {"="}), 5)
    return lines[:sep + 1], lines[sep + 1:]

def split_blocks(body_lines):
    blocks, cur = [], None
    for ln in body_lines:
        if not ln.strip():
            continue
        if TS_RE.match(ln):
            if cur is not None: blocks.append(cur)
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
    if cur is not None: blocks.append(cur)
    return blocks

def block_min(block):
    m = TS_RE.match(block[0]); return m.group(1) if m else ""

def block_text(block):
    return "\n".join(block)

def classify(text):
    for label, marker in TYPE_MARKERS:
        if marker in text:
            return label
    return "text"

def read_blocks(path):
    try:
        with open(path, errors="ignore") as f:
            header, body = split_header_body(f.read())
        return header, split_blocks(body)
    except Exception:
        return None, []

def file_last_min(path):
    _, blocks = read_blocks(path)
    return block_min(blocks[-1]) if blocks else ""

def update_header(header, count):
    out = []
    for l in header:
        if l.startswith("消息数量:"): out.append(f"消息数量: {count}")
        elif l.startswith("导出时间:"): out.append(f"导出时间: {datetime.now():%Y-%m-%d %H:%M}")
        else: out.append(l)
    return out

def fmt_breakdown(counter):
    abbr = {"text": "text", "image": "img", "link": "link", "video": "vid",
            "sticker": "sticker", "file": "file", "voice": "voice",
            "location": "loc", "transfer": "$", "redpacket": "红包",
            "card": "card", "music": "music", "system": "sys"}
    return " ".join(f"{abbr.get(k,k)}{v}" for k, v in counter.most_common())

# ---------- wechat-cli ----------

def cli_sessions(cfg):
    r = subprocess.run([cfg["wechat_cli"], "sessions", "--limit", "5000"],
                       capture_output=True, text=True, timeout=180)
    return json.loads(r.stdout)

def export_full(cfg, username, out_path):
    r = subprocess.run([cfg["wechat_cli"], "export", username, "--format", "txt",
                        "--limit", str(cfg["export_limit"]), "--output", out_path],
                       capture_output=True, text=True, timeout=cfg["per_chat_timeout"])
    out = (r.stdout + r.stderr).strip().replace("\n", " ")
    m = re.search(r"（(\d+)\s*条消息）", out)
    n = int(m.group(1)) if m else 0
    if "找不到" in out and n == 0: return ("empty", 0, out)
    if r.returncode == 0 and os.path.exists(out_path) and n > 0: return ("ok", n, out)
    return ("fail", n, out)

def export_incremental(cfg, username, path):
    """Fetch only messages since the file's last-seen minute and append them.
    Returns (status, total_count, added_blocks:list, breakdown:Counter)."""
    header, blocks = read_blocks(path)
    if not blocks:
        st, n, _ = export_full(cfg, username, path)
        return (st, n, [], Counter())
    last_min = block_min(blocks[-1])
    if not last_min:
        st, n, _ = export_full(cfg, username, path)
        return (st, n, [], Counter())
    tmp = path + ".inc"
    r = subprocess.run([cfg["wechat_cli"], "export", username, "--format", "txt",
                        "--start-time", last_min, "--limit", str(cfg["export_limit"]),
                        "--output", tmp],
                       capture_output=True, text=True, timeout=cfg["per_chat_timeout"])
    out = (r.stdout + r.stderr)
    if r.returncode != 0 or not os.path.exists(tmp):
        return ("ok", len(blocks), [], Counter())   # nothing new / boundary empty
    _, tblocks = read_blocks(tmp)
    try: os.remove(tmp)
    except OSError: pass
    # dedup the boundary minute against what we already have
    remaining = Counter(block_text(b) for b in blocks if block_min(b) == last_min)
    added = []
    for b in tblocks:
        bm, bt = block_min(b), block_text(b)
        if bm < last_min:
            continue
        if bm == last_min and remaining.get(bt, 0) > 0:
            remaining[bt] -= 1
            continue
        added.append(b)
    if not added:
        return ("ok", len(blocks), [], Counter())
    all_blocks = blocks + added
    new_header = update_header(header, len(all_blocks))
    with open(path, "w") as f:
        f.write("\n".join(new_header) + "\n")
        f.write("\n".join(block_text(b) for b in all_blocks) + "\n")
    breakdown = Counter(classify(block_text(b)) for b in added)
    return ("ok", len(all_blocks), added, breakdown)

# ---------- manifest / snapshot ----------

def header_count(path):
    try:
        with open(path, errors="ignore") as f:
            for _ in range(8):
                m = re.match(r"^消息数量:\s*(\d+)", f.readline())
                if m: return int(m.group(1))
    except Exception:
        pass
    return 0

def rebuild_manifest(cfg):
    all_dir = os.path.join(cfg["out_dir"], "all")
    rows = sorted(((header_count(os.path.join(all_dir, fn)), fn)
                   for fn in os.listdir(all_dir) if fn.endswith(".txt")),
                  key=lambda r: r[0], reverse=True)
    total = sum(n for n, _ in rows)
    with open(os.path.join(cfg["out_dir"], "MANIFEST.tsv"), "w") as f:
        f.write("msgs\tfile\n")
        for n, fn in rows: f.write(f"{n}\t{fn}\n")
    with open(os.path.join(cfg["out_dir"], "SUMMARY.md"), "w") as f:
        f.write(f"# WeChat export — {datetime.now():%Y-%m-%d %H:%M}\n\n"
                f"- Conversations with content: {sum(1 for n,_ in rows if n)}\n"
                f"- Total messages: {total:,}\n- Files in all/: {len(rows)}\n")
    return len(rows), total

def make_snapshot(cfg):
    stamp = datetime.now().strftime("%Y-%m-%d")
    snaps = os.path.join(cfg["out_dir"], "daily"); os.makedirs(snaps, exist_ok=True)
    path = os.path.join(snaps, f"{stamp}.tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        tar.add(os.path.join(cfg["out_dir"], "all"), arcname=f"wechat_{stamp}")
    cutoff = time.time() - cfg["retain_snapshots"] * 86400
    for old in glob.glob(os.path.join(snaps, "*.tar.gz")):
        if os.path.getmtime(old) < cutoff: os.remove(old)
    return path, os.path.getsize(path)

# ---------- run ----------

def decide_mode(cfg, state):
    if not state.get("last_full_at"): return "full"
    if (time.time() - state["last_full_at"]) / 86400 >= cfg["full_max_age_days"]:
        return "full"
    last_full_day = datetime.fromtimestamp(state["last_full_at"]).date()
    if datetime.now().weekday() == cfg["full_weekday"] and last_full_day < datetime.now().date():
        return "full"
    return "incremental"

def do_run(cfg, state, mode, verbose=True):
    out_dir = cfg["out_dir"]; all_dir = os.path.join(out_dir, "all")
    os.makedirs(all_dir, exist_ok=True)
    if not cfg["wechat_cli"]:
        log(out_dir, "FATAL: wechat-cli not found"); notify("WeChat export failed", "wechat-cli not found"); return 2
    log(out_dir, f"=== run start (mode={mode}) ===")
    if verbose: print(f"wechat-daily: {mode} run …")
    try:
        sessions = cli_sessions(cfg)
    except Exception as e:
        log(out_dir, f"FATAL: could not list sessions: {e}")
        notify("WeChat export failed", "Could not read WeChat — keys stale or Full Disk Access missing.")
        return 2
    log(out_dir, f"sessions: {len(sessions)}")

    chats = state.setdefault("chats", {})
    ok = empty = fail = skipped = 0
    added_total = Counter(); valid = set(); n = len(sessions)

    for i, s in enumerate(sessions, 1):
        user = s.get("username") or ""
        if not user: continue
        fname = chat_filename(s); valid.add(fname)
        path = os.path.join(all_dir, fname)
        last_ts = int(s.get("timestamp") or 0)
        name = s.get("chat") or user
        typ = "group" if s.get("is_group") else "dm"
        prev = chats.get(user, {})
        if not (mode == "full" or not os.path.exists(path) or last_ts > int(prev.get("last_msg_ts") or 0)):
            skipped += 1
            continue

        if mode == "full" or not os.path.exists(path):
            status, total, _ = export_full(cfg, user, path)
            added, breakdown = None, Counter()
        else:
            status, total, added, breakdown = export_incremental(cfg, user, path)
            added_total += breakdown

        if status == "ok":
            ok += 1
            chats[user] = {"last_msg_ts": last_ts, "file": fname, "msgs": total,
                           "last_min": file_last_min(path)}
            if verbose:
                if added is None:
                    print(f"  [{i:>3}/{n}] ✓ {name[:24]:24} {typ:5} {total:>7} msgs")
                elif added:
                    print(f"  [{i:>3}/{n}] ✓ {name[:24]:24} {typ:5} +{len(added):<4} {fmt_breakdown(breakdown)}")
                else:
                    print(f"  [{i:>3}/{n}] · {name[:24]:24} {typ:5} (no new)")
        elif status == "empty":
            empty += 1
            chats[user] = {"last_msg_ts": last_ts, "file": fname, "msgs": 0, "last_min": ""}
        else:
            fail += 1
            log(out_dir, f"  FAIL {name} ({user})")
            if verbose: print(f"  [{i:>3}/{n}] ✗ {name[:24]:24} {typ:5} FAILED")

    if mode == "full":
        for fn in os.listdir(all_dir):
            if fn.endswith(".txt") and fn not in valid:
                os.remove(os.path.join(all_dir, fn))

    nfiles, total = rebuild_manifest(cfg)
    snap, size = make_snapshot(cfg)
    summary = (f"exported_ok={ok} empty={empty} fail={fail} skipped={skipped} "
               f"files={nfiles} messages={total}")
    log(out_dir, "totals: " + summary)
    log(out_dir, f"snapshot: {snap} ({size//1024//1024} MB)")
    now = int(time.time())
    state["last_run_at"] = now; state["last_mode"] = mode
    if mode == "full": state["last_full_at"] = now
    if fail > max(10, len(sessions) // 5):
        notify("WeChat export degraded", f"{fail} chats failed — run: wechat-daily keys")
    _save(STATE_PATH, state)
    try:
        import streams
        streams.publish_due(cfg, verbose=verbose)
    except Exception as e:
        log(out_dir, f"streams publish error: {e}")
    log(out_dir, "=== run done ===")
    if verbose:
        print(f"\nDone ({mode}): {ok} updated, {skipped} idle skipped, {fail} failed.")
        if mode != "full" and added_total:
            print(f"New messages added: {sum(added_total.values())}  →  {fmt_breakdown(added_total)}")
        print(f"Snapshot: {snap}  ·  {nfiles} chats, {total:,} total messages")
    return 0

# ---------- visibility commands ----------

def find_files(cfg, query):
    all_dir = os.path.join(cfg["out_dir"], "all")
    if not os.path.isdir(all_dir): return []
    files = [fn for fn in os.listdir(all_dir) if fn.endswith(".txt")]
    if not query: return sorted(files)
    q = query.lower()
    return sorted(fn for fn in files if q in display_name(fn).lower() or q in fn.lower())

def cmd_list(cfg, query, limit):
    man = os.path.join(cfg["out_dir"], "MANIFEST.tsv")
    if not os.path.exists(man): print("No MANIFEST yet — run an export first."); return 1
    rows = []
    with open(man) as f:
        next(f, None)
        for line in f:
            n, _, fn = line.partition("\t"); fn = fn.strip()
            try: rows.append((int(n), fn))
            except ValueError: pass
    if query:
        q = query.lower(); rows = [r for r in rows if q in display_name(r[1]).lower()]
    print(f"{'#':>3}  {'msgs':>8}  {'type':5}  chat")
    for i, (n, fn) in enumerate(rows[:limit], 1):
        typ = fn.split("__")[0]
        print(f"{i:>3}  {n:>8}  {typ:5}  {display_name(fn)}")
    print(f"\n{len(rows)} chats" + (f" matching '{query}'" if query else "") +
          f"; showing {min(limit, len(rows))}.")
    return 0

def cmd_peek(cfg, query, limit):
    files = find_files(cfg, query)
    if not files:
        print(f"No chat matches '{query}'."); return 1
    if len(files) > 1:
        print(f"{len(files)} chats match '{query}' — be more specific:")
        for fn in files[:20]: print(f"  • {display_name(fn)}  ({header_count(os.path.join(cfg['out_dir'],'all',fn))} msgs)")
        return 1
    path = os.path.join(cfg["out_dir"], "all", files[0])
    _, blocks = read_blocks(path)
    print(f"# {display_name(files[0])}  ({len(blocks)} msgs, showing last {min(limit,len(blocks))})\n")
    for b in blocks[-limit:]:
        print(block_text(b))
    return 0

def cmd_stats(cfg, query):
    files = find_files(cfg, query)
    if not files: print("Nothing to analyze."); return 1
    scope = display_name(files[0]) if len(files) == 1 else (f"{len(files)} chats" + (f" matching '{query}'" if query else ""))
    types, first, last, total = Counter(), None, None, 0
    for fn in files:
        _, blocks = read_blocks(os.path.join(cfg["out_dir"], "all", fn))
        for b in blocks:
            types[classify(block_text(b))] += 1; total += 1
            mn = block_min(b)
            if mn:
                first = mn if first is None or mn < first else first
                last = mn if last is None or mn > last else last
    print(f"# stats — {scope}")
    print(f"  messages : {total:,}")
    if first: print(f"  range    : {first}  →  {last}")
    print(f"  by type  :")
    for k, v in types.most_common():
        bar = "█" * max(1, round(40 * v / total)) if total else ""
        print(f"    {k:9} {v:>8}  {100*v/total:5.1f}%  {bar}")
    return 0

# ---------- keys / agent / init / status ----------

def extract_keys(cfg):
    askpass = os.path.join(APP_DIR, "askpass.sh"); os.makedirs(APP_DIR, exist_ok=True)
    with open(askpass, "w") as f:
        f.write('#!/bin/sh\n/usr/bin/osascript -e \'Tell application "System Events" to '
                'display dialog "wechat-daily needs sudo to scan WeChat memory for DB keys.\\n\\n'
                'Enter your macOS login password:" default answer "" with hidden answer '
                'with title "wechat-daily" giving up after 120\' -e \'text returned of result\'\n')
    os.chmod(askpass, 0o755)
    env = dict(os.environ, SUDO_ASKPASS=askpass)
    print("A macOS password dialog will appear — enter your login password.")
    subprocess.run(["sudo", "-A", cfg["wechat_cli"], "init", "--force", "--db-dir", cfg["db_dir"]], env=env)
    keys = os.path.join(HOME, ".wechat-cli", "all_keys.json")
    n = len(re.findall(r"message/message_\d+\.db", open(keys).read())) if os.path.exists(keys) else 0
    print(f"\nKeys: {n} message-DB keys present "
          f"({'OK' if n else 'NONE — open WeChat, scroll some chats, retry'})")
    return n > 0

def install_agent(cfg):
    node_dir = os.path.dirname(cfg["wechat_cli"]) if cfg["wechat_cli"] else "/usr/local/bin"
    wrapper = os.path.join(APP_DIR, "run.sh"); os.makedirs(APP_DIR, exist_ok=True)
    with open(wrapper, "w") as f:
        f.write("#!/bin/bash\n"
                f'export PATH="{node_dir}:/usr/bin:/bin:/usr/sbin:/sbin"\n'
                f'exec /usr/bin/python3 "{os.path.join(SRC_DIR, "wechat_daily.py")}" run --auto\n')
    os.chmod(wrapper, 0o755)
    out_logs = os.path.join(cfg["out_dir"], "logs"); os.makedirs(out_logs, exist_ok=True)
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "w") as f:
        f.write(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{AGENT_LABEL}</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>{wrapper}</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{cfg["hour"]}</integer><key>Minute</key><integer>{cfg["minute"]}</integer></dict>
  <key>StandardOutPath</key><string>{os.path.join(out_logs, "agent.out.log")}</string>
  <key>StandardErrorPath</key><string>{os.path.join(out_logs, "agent.err.log")}</string>
  <key>RunAtLoad</key><false/>
</dict></plist>
''')
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", PLIST_PATH], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", PLIST_PATH], capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout).strip()

def uninstall_agent():
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", PLIST_PATH], capture_output=True)
    if os.path.exists(PLIST_PATH): os.remove(PLIST_PATH)

def keycount():
    keys = os.path.join(HOME, ".wechat-cli", "all_keys.json")
    return len(re.findall(r"message/message_\d+\.db", open(keys).read())) if os.path.exists(keys) else 0

def cmd_status(cfg, state):
    def ts(x): return datetime.fromtimestamp(x).strftime("%Y-%m-%d %H:%M") if x else "never"
    uid = os.getuid()
    loaded = subprocess.run(["launchctl", "print", f"gui/{uid}/{AGENT_LABEL}"],
                            capture_output=True, text=True).returncode == 0
    nk = keycount()
    print("wechat-daily status")
    print(f"  wechat-cli : {cfg['wechat_cli'] or 'NOT FOUND'}")
    print(f"  out_dir    : {cfg['out_dir']}")
    print(f"  db keys    : {nk} message-DB keys {'(ok)' if nk else '(MISSING — run: wechat-daily keys)'}")
    print(f"  last run   : {ts(state.get('last_run_at',0))} (mode={state.get('last_mode','-')})")
    print(f"  last full  : {ts(state.get('last_full_at',0))}")
    print(f"  next mode  : {decide_mode(cfg, state)}")
    print(f"  agent      : {('daily at %02d:%02d' % (cfg['hour'], cfg['minute'])) if loaded else 'NOT installed (wechat-daily install-agent)'}")
    alld = os.path.join(cfg["out_dir"], "all")
    if os.path.isdir(alld):
        print(f"  files      : {len([f for f in os.listdir(alld) if f.endswith('.txt')])} chats in all/")
    return 0

def cmd_init(cfg):
    print("== wechat-daily init ==")
    cfg = load_config()
    if not cfg["wechat_cli"]:
        print("✗ wechat-cli not found.  npm install -g @canghe_ai/wechat-cli"); return 1
    print(f"✓ wechat-cli: {cfg['wechat_cli']}")
    if not cfg["db_dir"]:
        print("✗ WeChat db_storage not found — is WeChat installed and logged in?"); return 1
    print(f"✓ WeChat data: {cfg['db_dir']}")
    _save(CONFIG_PATH, {k: cfg[k] for k in DEFAULTS})
    if not keycount():
        print("• No DB keys — extracting (sudo + WeChat open)…")
        if not extract_keys(cfg):
            print("✗ incomplete. Open WeChat, scroll chats, then: wechat-daily keys"); return 1
    else:
        print(f"✓ DB keys present ({keycount()} message DBs)")
    ok, msg = install_agent(cfg)
    print("✓ agent installed" if ok else f"✗ agent install failed: {msg}")
    node = cfg["wechat_cli"].replace("/bin/wechat-cli", "/bin/node")
    print("\nIMPORTANT — grant Full Disk Access (System Settings → Privacy & Security) to BOTH:")
    print(f"    {node}\n    {cfg['wechat_cli']}")
    print("\nNext:  wechat-daily run --full     (first full backfill)")
    return 0

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(prog="wechat-daily", description="Headless daily WeChat history exporter.")
    sub = ap.add_subparsers(dest="cmd")
    pr = sub.add_parser("run", help="export now"); g = pr.add_mutually_exclusive_group()
    g.add_argument("--auto", action="store_true"); g.add_argument("--full", action="store_true")
    g.add_argument("--incremental", action="store_true")
    pr.add_argument("--quiet", action="store_true")
    pl = sub.add_parser("list", help="chats by message count")
    pl.add_argument("query", nargs="?"); pl.add_argument("--limit", type=int, default=30)
    pp = sub.add_parser("peek", help="recent messages of a chat")
    pp.add_argument("query"); pp.add_argument("--limit", type=int, default=30)
    ps = sub.add_parser("stats", help="message-type breakdown")
    ps.add_argument("query", nargs="?")
    for c in ("status", "init", "keys", "install-agent", "uninstall-agent", "uninstall"):
        sub.add_parser(c)
    pc = sub.add_parser("config"); pc.add_argument("key", nargs="?"); pc.add_argument("value", nargs="?")
    pstm = sub.add_parser("streams", help="publish WeChat groups as queryable Router feeds")
    pstm.add_argument("action", nargs="?", default="ui",
                      choices=["ui", "list", "status", "add", "config", "preview", "publish", "remove"])
    pstm.add_argument("rest", nargs="*")
    pstm.add_argument("--dry", action="store_true", help="preview instead of publishing")
    pstm.add_argument("--day", action="store_true", help="publish the latest full day, not just new-since-last")
    args = ap.parse_args()
    cfg, state = load_config(), load_state()

    if args.cmd == "streams":
        import streams
        return streams.dispatch(cfg, args)

    if args.cmd == "run":
        mode = "full" if args.full else "incremental" if args.incremental else decide_mode(cfg, state)
        return do_run(cfg, state, mode, verbose=not args.quiet)
    if args.cmd == "list":   return cmd_list(cfg, args.query, args.limit)
    if args.cmd == "peek":   return cmd_peek(cfg, args.query, args.limit)
    if args.cmd == "stats":  return cmd_stats(cfg, args.query)
    if args.cmd == "status": return cmd_status(cfg, state)
    if args.cmd == "init":   return cmd_init(cfg)
    if args.cmd == "keys":   return 0 if extract_keys(cfg) else 1
    if args.cmd == "install-agent":
        ok, msg = install_agent(cfg); print("installed" if ok else f"failed: {msg}"); return 0 if ok else 1
    if args.cmd == "uninstall-agent": uninstall_agent(); print("agent removed"); return 0
    if args.cmd == "uninstall":
        uninstall_agent()
        for p in (CONFIG_PATH, STATE_PATH, os.path.join(APP_DIR, "run.sh"), os.path.join(APP_DIR, "askpass.sh")):
            if os.path.exists(p): os.remove(p)
        print(f"Removed agent + state. Exports in {cfg['out_dir']} kept."); return 0
    if args.cmd == "config":
        if not args.key:
            print(json.dumps({k: cfg[k] for k in DEFAULTS}, indent=2, ensure_ascii=False)); return 0
        saved = _load(CONFIG_PATH, {}); val = args.value
        if val is not None and re.fullmatch(r"-?\d+", val): val = int(val)
        saved[args.key] = val; _save(CONFIG_PATH, saved); print(f"{args.key} = {val}"); return 0
    ap.print_help(); return 0

if __name__ == "__main__":
    sys.exit(main() or 0)
