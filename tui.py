#!/usr/bin/env python3
"""
wechat-daily streams — terminal cockpit (curses, stdlib only).

Two panes: LIBRARY (your chats) on the left, STREAM CONFIG + live PREVIEW on the
right. Promote a chat to a stream, tune its policy, see exactly what would publish,
and publish — all from the keyboard.

Opened by `wechat-daily streams` (no args). Not run directly.
"""
from __future__ import annotations
import curses, os
from collections import Counter
from datetime import datetime

import wechat_daily as wd
import streams as st

def what_text(s):
    """Plain-language description of what this stream publishes."""
    if s.get("curation") == "agent":
        lens = s.get("lens") or "market intelligence"
        return f"AI-interpreted daily intel — lens: {lens} ({s.get('agent_model','sonnet')})"
    base = "every message, verbatim" if s.get("curation") == "raw" else "messages, anonymized"
    idmap = {"real": "with sender names", "pseudonymize": "senders → Member-A,B…",
             "drop": "senders removed"}
    extra = idmap.get(s.get("identity"), "")
    red = s.get("redact") or []
    if red: extra += f", redact {'/'.join(red)}"
    if s.get("drop_system"): extra += ", no system msgs"
    return f"{base}, {extra}"

# config rows shown in the right pane
FIELDS = [
    ("status",       "status",            "choice", ["active", "paused"]),
    ("curation",     "curation",          "choice", ["raw", "anonymized", "agent"]),
    ("identity",     "identity",          "choice", ["real", "pseudonymize", "drop"]),
    ("agent lens",   "lens",              "text",   None),
    ("agent model",  "agent_model",       "choice", ["sonnet", "opus", "haiku"]),
    ("redact phone", ("redact", "phone"),     "flag", None),
    ("redact email", ("redact", "email"),     "flag", None),
    ("redact wxid",  ("redact", "wechat_id"), "flag", None),
    ("drop system",  "drop_system",       "bool",   None),
    ("chunk size",   "chunk_size",        "int",    None),
    ("channel",      "channel",           "text",   None),
    ("tags",         "tags",              "textlist", None),
]

# ---------- display-width helpers (CJK = 2 cells) ----------

def cwidth(ch):
    o = ord(ch)
    if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
            or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFF60
            or 0xFFE0 <= o <= 0xFFE6 or 0x1F300 <= o <= 0x1FAFF or 0x20000 <= o <= 0x3FFFD):
        return 2
    return 1

def dwidth(s): return sum(cwidth(c) for c in s)

def trunc(s, width):
    out, w = [], 0
    for c in s:
        cw = cwidth(c)
        if w + cw > width: break
        out.append(c); w += cw
    return "".join(out), w

def addstr(win, y, x, s, w, attr=0):
    s, _ = trunc(s, max(0, w))
    try: win.addstr(y, x, s, attr)
    except curses.error: pass

# ---------- app state ----------

class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.chats = self._load_chats()       # [(file, name, type, msgs)]
        self.streams = self._load_streams()   # file -> config
        self.sel = 0
        self.top = 0
        self.focus = "lib"                     # lib | cfg
        self.crow = 0
        self.preview_cache = {}
        self.status = "↑↓ move · space promote · tab → config · p publish · q quit"

    def _load_chats(self):
        man = os.path.join(self.cfg["out_dir"], "MANIFEST.tsv")
        rows = []
        if os.path.exists(man):
            with open(man) as f:
                next(f, None)
                for line in f:
                    n, _, fn = line.partition("\t"); fn = fn.strip()
                    if not fn: continue
                    try: msgs = int(n)
                    except ValueError: continue
                    rows.append((fn, wd.display_name(fn), fn.split("__")[0], msgs))
        return rows

    def _load_streams(self):
        return {s["source_file"]: s for s in st.load_all()}

    def cur(self):
        return self.chats[self.sel] if self.chats else None

    def cur_stream(self):
        c = self.cur()
        return self.streams.get(c[0]) if c else None

    # ----- preview -----
    def preview(self, force=False):
        c = self.cur(); s = self.cur_stream()
        if not c or not s: return None
        key = (c[0], s["curation"], s["identity"], tuple(s.get("redact", [])),
               s.get("drop_system"), s.get("chunk_size"), s.get("last_published_at"))
        if not force and key in self.preview_cache:
            return self.preview_cache[key]
        path = os.path.join(self.cfg["out_dir"], "all", c[0])
        _, blocks = wd.read_blocks(path)
        sample = st.window(blocks, latest_day=True)          # a representative day
        lines, stats = st.curate(s, sample)
        cs = max(1, s.get("chunk_size", 1000))
        since = int(s.get("last_published_at", 0) or 0)
        first = since <= 0
        nxt = sample if first else st.window(blocks, since_ts=since)  # what the NEXT push contains
        res = {"lines": lines, "stats": stats,
               "sample_n": len(sample),
               "day_chunks": -(-len(sample) // cs) if sample else 0,
               "first": first, "next_n": len(nxt),
               "next_chunks": -(-len(nxt) // cs) if nxt else 0,
               "range": (wd.block_min(sample[0]) if sample else "",
                         wd.block_min(sample[-1]) if sample else ""),
               "last_pub": since}
        self.preview_cache[key] = res
        return res

# ---------- field get/set ----------

def field_val(s, key):
    if isinstance(key, tuple):
        return key[1] in s.get(key[0], [])
    return s.get(key)

def field_display(s, label, key, kind):
    v = field_val(s, key)
    if kind in ("flag", "bool"):
        return f"[{'✓' if v else ' '}] {label}"
    if kind == "choice":
        return f"{label:13} ‹ {v} ›"
    if kind == "textlist":
        return f"{label:13} {','.join(v or [])}"
    return f"{label:13} {v}"

def field_activate(app, s, label, key, kind, opts, delta, stdscr):
    if kind in ("flag",):
        lst = s.setdefault(key[0], [])
        if key[1] in lst: lst.remove(key[1])
        else: lst.append(key[1])
    elif kind == "bool":
        s[key] = not s.get(key)
    elif kind == "choice":
        i = (opts.index(s.get(key, opts[0])) + (delta or 1)) % len(opts)
        s[key] = opts[i]
    elif kind == "int":
        step = 250
        s[key] = max(50, int(s.get(key, 1000)) + (delta or 1) * step)
    elif kind in ("text", "textlist"):
        new = edit_line(stdscr, f"{label}: ", _as_text(s.get(key), kind))
        if new is not None:
            s[key] = [x for x in _split(new)] if kind == "textlist" else new
    st.save_one(s)
    app.preview_cache.clear()

def _as_text(v, kind): return ",".join(v or []) if kind == "textlist" else (v or "")
def _split(t):
    import re
    return [x for x in re.split(r"[,\s]+", t) if x]

def edit_line(stdscr, prompt, current):
    h, w = stdscr.getmaxyx()
    curses.curs_set(1); curses.echo()
    win = curses.newwin(3, w - 4, h // 2 - 1, 2)
    win.box(); win.addstr(0, 2, " edit (enter=save, esc=cancel) ")
    addstr(win, 1, 2, prompt, w - 8)
    win.move(1, 2 + dwidth(prompt))
    try: win.addstr(1, 2 + dwidth(prompt), current[: w - 12])
    except curses.error: pass
    win.refresh()
    curses.curs_set(1)
    try:
        s = win.getstr(1, 2 + dwidth(prompt), 200).decode("utf-8", "ignore")
    except Exception:
        s = None
    curses.noecho(); curses.curs_set(0)
    return s if s != "" or current == "" else s

# ---------- drawing ----------

def draw(stdscr, app):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    lw = max(34, w * 4 // 10)        # left pane width
    # header
    addstr(stdscr, 0, 0, " wechat-daily streams".ljust(w), w, curses.A_REVERSE | curses.A_BOLD)
    legend = "● streaming   ○ local only "
    addstr(stdscr, 0, max(0, w - dwidth(legend) - 1), legend, dwidth(legend) + 1,
           curses.A_REVERSE | curses.A_BOLD)
    # left pane
    addstr(stdscr, 1, 1, "LIBRARY", lw, curses.A_BOLD)
    addstr(stdscr, 1, lw - 12, f"{len(app.streams)} streams", 12, curses.A_DIM)
    rows = h - 4
    if app.sel < app.top: app.top = app.sel
    if app.sel >= app.top + rows: app.top = app.sel - rows + 1
    for i in range(app.top, min(len(app.chats), app.top + rows)):
        fn, name, typ, msgs = app.chats[i]
        y = 2 + (i - app.top)
        is_stream = fn in app.streams
        paused = is_stream and app.streams[fn].get("status") == "paused"
        mark = "●" if (is_stream and not paused) else ("◍" if paused else "○")
        cnt = f"{msgs//1000}k" if msgs >= 1000 else str(msgs)
        attr = curses.A_REVERSE if (i == app.sel and app.focus == "lib") else 0
        if i == app.sel and app.focus != "lib": attr = curses.A_BOLD
        mcol = curses.color_pair(1) if is_stream and not paused else 0
        addstr(stdscr, y, 1, mark, 2, mcol | attr)
        addstr(stdscr, y, 3, name, lw - 12, attr)
        addstr(stdscr, y, lw - 9, f"{cnt:>5} {typ:5}", 9, attr | curses.A_DIM)
    # divider
    for y in range(1, h - 1):
        addstr(stdscr, y, lw, "│", 1, curses.A_DIM)
    # right pane
    rx = lw + 2; rw = w - rx - 1
    c = app.cur()
    if not c:
        addstr(stdscr, 2, rx, "No chats. Run an export first.", rw); _footer(stdscr, app); stdscr.refresh(); return
    s = app.cur_stream()
    addstr(stdscr, 1, rx, f"STREAM: {c[1]}", rw, curses.A_BOLD)
    if not s:
        addstr(stdscr, 3, rx, "○ Local only — not published.", rw)
        addstr(stdscr, 5, rx, "Press [space] to make this chat a Router stream:", rw, curses.A_DIM)
        addstr(stdscr, 6, rx, "its new messages get pushed daily to a channel", rw, curses.A_DIM)
        addstr(stdscr, 7, rx, "anyone on Router can search & follow.", rw, curses.A_DIM)
        _footer(stdscr, app); stdscr.refresh(); return
    pv = app.preview()
    live = s.get("status") == "active"
    addstr(stdscr, 2, rx, "● LIVE — publishing to Router" if live else "◍ PAUSED — not publishing",
           rw, (curses.color_pair(1) | curses.A_BOLD) if live else curses.A_DIM)
    sched = f"every day ~{app.cfg['hour']:02d}:{app.cfg['minute']:02d}, automatically" if live else "paused"
    rows = [("What: ", what_text(s)),
            ("Where:", f"#{s.get('channel')} — searchable/followable by anyone"),
            ("When: ", sched)]
    y = 4
    for tag, val in rows:
        addstr(stdscr, y, rx, tag, 7, curses.A_BOLD)
        addstr(stdscr, y, rx + 7, val, rw - 7); y += 1
    if pv:
        if pv["first"]:
            nxt = f"first push = a full day (~{pv['next_n']} msgs → {pv['next_chunks']} entries)"
        elif pv["next_n"]:
            nxt = f"{pv['next_n']} new since last → {pv['next_chunks']} entries"
        else:
            nxt = "nothing new since last publish (caught up)"
        addstr(stdscr, y, rx, "Next: ", 7, curses.A_BOLD)
        addstr(stdscr, y, rx + 7, nxt, rw - 7, curses.color_pair(2)); y += 1
        lp = datetime.fromtimestamp(pv["last_pub"]).strftime("%m-%d %H:%M") if pv["last_pub"] else "never"
        addstr(stdscr, y, rx, f"       last published: {lp}", rw, curses.A_DIM); y += 1
    y += 1
    # config fields
    addstr(stdscr, y, rx, "── settings (tab to edit) ──", rw, curses.A_DIM); y += 1
    cfg_top = y
    for idx, (label, key, kind, opts) in enumerate(FIELDS):
        yy = cfg_top + idx
        sel = (app.focus == "cfg" and idx == app.crow)
        addstr(stdscr, yy, rx, ("▸ " if sel else "  ") + field_display(s, label, key, kind),
               rw, curses.A_REVERSE if sel else 0)
    y = cfg_top + len(FIELDS) + 1
    # sample
    addstr(stdscr, y, rx, "── SAMPLE: what a day of this looks like ──", rw, curses.A_BOLD); y += 1
    if pv:
        rng = f"{pv['range'][0][5:]}–{pv['range'][1][11:]}" if pv["range"][0] else ""
        addstr(stdscr, y, rx, f"{pv['sample_n']} msgs · {rng} · ≈{pv['day_chunks']} entries/day",
               rw, curses.color_pair(2)); y += 1
        extra = []
        if pv["stats"]["aliased"]: extra.append(f"aliased {pv['stats']['aliased']}")
        if pv["stats"]["stripped"]: extra.append(f"stripped {pv['stats']['stripped']}")
        if extra: addstr(stdscr, y, rx, "🔒 " + " · ".join(extra), rw, curses.A_DIM); y += 1
        for j, line in enumerate(pv["lines"][:max(0, h - 2 - y)]):
            addstr(stdscr, y + j, rx, line, rw, curses.A_DIM)
    _footer(stdscr, app)
    stdscr.refresh()

def _footer(stdscr, app):
    h, w = stdscr.getmaxyx()
    addstr(stdscr, h - 1, 0, " " + app.status, w, curses.A_REVERSE)

# ---------- input ----------

def handle(stdscr, app, ch):
    if ch in (ord("q"), 27):
        return False
    if ch == curses.KEY_RESIZE:
        return True
    if ch == 9:  # TAB
        if app.cur_stream(): app.focus = "cfg" if app.focus == "lib" else "lib"
        return True
    if app.focus == "lib":
        if ch in (curses.KEY_UP, ord("k")): app.sel = max(0, app.sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")): app.sel = min(len(app.chats) - 1, app.sel + 1)
        elif ch == ord(" "):
            _toggle_promote(app)
        elif ch in (10, 13, curses.KEY_RIGHT):
            if app.cur_stream(): app.focus = "cfg"
        elif ch == ord("p"): _publish(stdscr, app)
        elif ch == ord("d"): app.preview(force=True); app.status = "preview refreshed"
    else:  # cfg
        if ch in (curses.KEY_UP, ord("k")): app.crow = max(0, app.crow - 1)
        elif ch in (curses.KEY_DOWN, ord("j")): app.crow = min(len(FIELDS) - 1, app.crow + 1)
        elif ch in (ord(" "), 10, 13, curses.KEY_RIGHT):
            _activate(app, stdscr, +1)
        elif ch == curses.KEY_LEFT:
            _activate(app, stdscr, -1)
        elif ch == ord("p"): _publish(stdscr, app)
        elif ch == ord("d"): app.preview(force=True)
        elif ch == curses.KEY_LEFT and app.crow == 0: app.focus = "lib"
    return True

def _toggle_promote(app):
    c = app.cur()
    if not c: return
    if c[0] in app.streams:
        st.cmd_remove(app.cfg, app.streams[c[0]]["id"])
        del app.streams[c[0]]
        app.status = f"removed stream · {c[1]}"
    else:
        s = st.build_default(c[0], c[1]); st.save_one(s)
        app.streams[c[0]] = s
        app.status = f"promoted → {s['channel']}"
    app.preview_cache.clear()

def _activate(app, stdscr, delta):
    s = app.cur_stream()
    if not s: return
    label, key, kind, opts = FIELDS[app.crow]
    field_activate(app, s, label, key, kind, opts, delta, stdscr)
    app.streams[s["source_file"]] = s
    app.status = "saved"

def _publish(stdscr, app):
    s = app.cur_stream()
    if not s:
        app.status = "promote it first (space)"; return
    if not _confirm(stdscr, f"Publish '{s['name']}' new messages to {s['channel']}?"):
        app.status = "publish cancelled"; return
    app.status = "publishing…"; draw(stdscr, app)
    try:
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            n = st.publish_stream(app.cfg, s, verbose=True)
        app.streams[s["source_file"]] = st.load_one(s["id"]) or s
        app.status = f"published {n} entr{'y' if n==1 else 'ies'} → {s['channel']}"
    except Exception as e:
        app.status = f"publish error: {e}"

def _confirm(stdscr, msg):
    h, w = stdscr.getmaxyx()
    win = curses.newwin(3, min(w - 4, dwidth(msg) + 12), h // 2 - 1, 2)
    win.box(); addstr(win, 1, 2, msg + "  [y/n]", w - 8)
    win.refresh()
    while True:
        k = win.getch()
        if k in (ord("y"), ord("Y")): return True
        if k in (ord("n"), ord("N"), 27): return False

# ---------- entry ----------

def _main(stdscr, cfg):
    curses.curs_set(0)
    curses.use_default_colors()
    try:
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
    except curses.error:
        pass
    app = App(cfg)
    running = True
    while running:
        draw(stdscr, app)
        ch = stdscr.getch()
        running = handle(stdscr, app, ch)

def run(cfg):
    import locale
    locale.setlocale(locale.LC_ALL, "")  # required for CJK rendering in curses
    if not App(cfg).chats:
        print("No exported chats yet. Run:  wechat-daily run --full")
        return 1
    curses.wrapper(_main, cfg)
    return 0
