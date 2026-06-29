#!/usr/bin/env python3
"""
wechat-daily rednote — daily RedNote market-research streams via OpenCLI.

Each RedNote stream is a search query plus an analyst lens. A publish run uses
OpenCLI's built-in `rednote` adapter against your logged-in Chrome session,
asks local Claude Code to turn the results into a daily intel update, and
publishes the markdown to Router.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from urllib.parse import quote_plus

import streams as st
import wechat_daily as wd

REDNOTE_DIR = os.path.join(wd.APP_DIR, "rednote")
DEFAULT_TAGS = ["stream", "rednote", "market-research"]

REDNOTE_DEFAULTS = {
    "query": "",
    "adapter": "rednote",
    "browser_session": "",
    "limit": 20,
    "detail_limit": 5,
    "lens": "market intelligence — products, companies, creators, sentiment, tools, and buying intent",
    "agent_model": "sonnet",
    "language": "",
    "source_note": "Source: RedNote search results collected through your logged-in browser via OpenCLI.",
    "tags": DEFAULT_TAGS,
    "status": "active",
    "last_published_at": 0,
}

OPENCLI = os.environ.get("WECHAT_DAILY_OPENCLI") or shutil.which("opencli") or "opencli"
CLAUDE = os.environ.get("WECHAT_DAILY_CLAUDE") or shutil.which("claude") or "claude"
REDNOTE_SESSION_ARGS = ["--site-session", "persistent"]
BROWSER_SEARCH_SESSION = "wd-rednote"

ADAPTER_HOSTS = {
    "rednote": "www.rednote.com",
    "xiaohongshu": "www.xiaohongshu.com",
}


def _dir():
    os.makedirs(REDNOTE_DIR, exist_ok=True)
    return REDNOTE_DIR


def _path(sid):
    return os.path.join(_dir(), f"{sid}.json")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "rednote"


def load_all():
    out = []
    for name in sorted(os.listdir(_dir())):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(_dir(), name)) as f:
                out.append(json.load(f))
        except Exception:
            pass
    return out


def load_one(sid):
    p = _path(sid)
    return json.load(open(p)) if os.path.exists(p) else None


def save_one(c):
    with open(_path(c["id"]), "w") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)


def _split_csv(val):
    return [x for x in re.split(r"[,\s]+", val or "") if x]


def _extract_json(stdout):
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=-1)
    if start < 0:
        raise RuntimeError("OpenCLI did not return JSON. Try running with `opencli rednote search <query> -f json`.")
    return json.loads(text[start:])


def _opencli_json(args, timeout=180):
    cmd = [OPENCLI] + args + REDNOTE_SESSION_ARGS + ["-f", "json"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(msg or f"OpenCLI command failed: {' '.join(cmd)}")
    return _extract_json(r.stdout)


def _browser_json(args, timeout=180):
    cmd = [OPENCLI, "browser"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        raise RuntimeError(msg or f"OpenCLI browser command failed: {' '.join(cmd)}")
    return _extract_json(r.stdout)


def _adapter_name(stream):
    name = (stream.get("adapter") or "rednote").strip().lower()
    if name not in ADAPTER_HOSTS:
        raise RuntimeError(f"Unsupported RedNote adapter '{name}'. Use rednote or xiaohongshu.")
    return name


def _browser_search(query, limit, adapter, session_name=""):
    host = ADAPTER_HOSTS[adapter]
    url = f"https://{host}/search_result/?keyword={quote_plus(query)}&source=web_search_result_notes"
    session = (session_name or f"{BROWSER_SEARCH_SESSION}-{adapter}").strip()
    subprocess.run([OPENCLI, "browser", session, "open", url, "--window", "background"],
                   capture_output=True, text=True, timeout=90)
    subprocess.run([OPENCLI, "browser", session, "wait", "time", "6"],
                   capture_output=True, text=True, timeout=30)
    js = r"""
(() => {
  const out = [];
  const sections = Array.from(document.querySelectorAll('section'));
  for (const section of sections) {
    const links = Array.from(section.querySelectorAll('a')).map((a) => ({
      href: a.href || '',
      text: (a.innerText || '').trim(),
    }));
    const titleLink = links.find((l) => l.text && /\/(search_result|explore)\//.test(l.href));
    const rawText = (section.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean);
    const authorLink = links.find((l) => /\/user\/profile\//.test(l.href));
    const authorParts = (authorLink?.text || '').split('\n').map((s) => s.trim()).filter(Boolean);
    const title = titleLink?.text || rawText[0] || '';
    const noteUrl = titleLink?.href || links.find((l) => /\/(search_result|explore)\//.test(l.href))?.href || '';
    if (!title || !noteUrl) continue;
    out.push({
      rank: out.length + 1,
      title,
      author: authorParts[0] || rawText[1] || '',
      author_url: authorLink?.href || '',
      likes: rawText[rawText.length - 1] || '',
      published_at: authorParts[1] || rawText[2] || '',
      url: noteUrl,
    });
  }
  return out;
})()
"""
    rows = _browser_json([session, "eval", js], timeout=120)
    if not isinstance(rows, list):
        raise RuntimeError("OpenCLI browser search returned a non-list JSON payload")
    return rows[:limit]


def _tool_version(cmd):
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return (r.stdout or r.stderr or "").strip().splitlines()[0]
        return ""
    except Exception:
        return ""


def cmd_check():
    ok = True
    print("wechat-daily rednote check")
    for label, cmd, hint in (
        ("opencli", OPENCLI, "npm install -g @jackwener/opencli"),
        ("claude", CLAUDE, "Install Claude Code and ensure `claude` is on PATH"),
        ("router", st.router_bin(), "Install/init the router CLI"),
    ):
        version = _tool_version(cmd)
        if version:
            print(f"  ✓ {label:7} {cmd}  ({version})")
        else:
            ok = False
            print(f"  ✗ {label:7} not available via {cmd!r}")
            print(f"          {hint}")
    print("  • Chrome must be logged into https://www.rednote.com or https://www.xiaohongshu.com")
    print("  • OpenCLI Browser Bridge must be installed and connected")
    print("  • This integration uses `--site-session persistent`; run `opencli rednote login --site-session persistent` if auth fails")
    return 0 if ok else 1


def collect(stream):
    """Collect RedNote search rows and optional note details through OpenCLI."""
    query = stream.get("query") or stream.get("name") or ""
    adapter = _adapter_name(stream)
    limit = max(1, min(100, int(stream.get("limit") or 20)))
    detail_limit = max(0, min(limit, int(stream.get("detail_limit") or 0)))
    search_error = None
    try:
        rows = _opencli_json([adapter, "search", query, "--limit", str(limit)])
    except Exception as e:
        search_error = str(e)
        rows = []
    if not isinstance(rows, list):
        raise RuntimeError(f"OpenCLI {adapter} search returned a non-list JSON payload")
    if not rows:
        rows = _browser_search(query, limit, adapter, stream.get("browser_session") or "")
    if not rows and search_error:
        raise RuntimeError(search_error)
    rows = rows[:limit]
    details = []
    for row in rows[:detail_limit]:
        url = row.get("url") if isinstance(row, dict) else None
        if not url:
            continue
        try:
            note_rows = _opencli_json([adapter, "note", url], timeout=240)
            details.append({"url": url, "rows": note_rows})
        except Exception as e:
            details.append({"url": url, "error": str(e)[:300]})
    return {
        "query": query,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "search_results": rows,
        "note_details": details,
    }


def brief_path(stream):
    return os.path.join(_dir(), f"{stream['id']}.brief.md")


def run_agent(stream, payload):
    bp = brief_path(stream)
    brief = open(bp).read() if os.path.exists(bp) else ""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        data_path = f.name
    lens = stream.get("lens") or REDNOTE_DEFAULTS["lens"]
    model = stream.get("agent_model", "sonnet")
    language = (stream.get("language") or "").strip()
    source_note = (stream.get("source_note") or "").strip()
    prompt = (
        f"Read the RedNote collection JSON at {data_path}.\n"
        "Produce a DAILY REDNOTE MARKET INTEL UPDATE for subscribers of this stream.\n"
        + (f"Write the entire update in {language}.\n" if language else "")
        + (f"Make this source context explicit in the update: {source_note}\n" if source_note else "")
        + "Sections: **TL;DR** (1-2 lines), **Notable notes**, **Signals & deltas**, "
        "**People/brands/products to watch**, **Open questions**, **Links**.\n"
        "Rules: be signal-dense; keep RedNote URLs; distinguish direct evidence from inference; "
        "do not invent metrics; summarize repeated themes instead of listing every note.\n"
        + (f"\nPrior running brief for continuity/deltas:\n{brief}\n" if brief else "")
        + "\nAfter the update, print a line containing exactly <<<BRIEF>>> then a concise "
        "updated running brief for the next run.\n"
        "Output clean markdown only, no preamble."
    )
    sysp = ("You are a sharp market-intelligence analyst turning RedNote search results "
            f"into dense, queryable daily signal. Lens: {lens}.")
    try:
        r = subprocess.run([CLAUDE, "-p", prompt, "--model", model,
                            "--allowedTools", "Read", "WebSearch",
                            "--append-system-prompt", sysp],
                           capture_output=True, text=True, timeout=900,
                           stdin=subprocess.DEVNULL)
    finally:
        try:
            os.unlink(data_path)
        except OSError:
            pass
    out = (r.stdout or "").strip()
    low = out.lower()
    if (not out or len(out) < 200
            or any(p in low for p in ("session limit", "hit your", "usage limit", "rate limit"))
            or out.startswith("Execution error")):
        return "", False
    intel, _, newbrief = out.partition("<<<BRIEF>>>")
    if newbrief.strip():
        with open(bp, "w") as f:
            f.write(newbrief.strip() + "\n")
    if source_note:
        intel = f"> {source_note}\n\n{intel.strip()}"
    return intel.strip(), True


def cmd_add(query, sid=None):
    query = (query or "").strip()
    if not query:
        print("Usage: wechat-daily rednote add <query>")
        return 1
    sid = sid or slugify(query)
    if load_one(sid):
        print(f"RedNote stream '{sid}' already exists. Use: wechat-daily rednote config {sid} <key> <val>")
        return 1
    s = dict(REDNOTE_DEFAULTS)
    s.update({"id": sid, "name": query, "query": query, "channel": "rednote-" + sid})
    save_one(s)
    print(f"✓ rednote stream '{sid}' created")
    print(f"  query={query!r} limit={s['limit']} detail_limit={s['detail_limit']} channel={s['channel']}")
    print(f"  preview: wechat-daily rednote preview {sid}")
    return 0


def cmd_list():
    rows = load_all()
    if not rows:
        print("No RedNote streams yet. Add one: wechat-daily rednote add <query>")
        return 0
    print(f"{'id':28} {'status':8} {'limit':>5} {'details':>7} channel")
    for s in rows:
        when = datetime.fromtimestamp(s["last_published_at"]).strftime("%m-%d %H:%M") if s.get("last_published_at") else "never"
        print(f"{s['id'][:28]:28} {s.get('status','active'):8} {int(s.get('limit', 20)):>5} "
              f"{int(s.get('detail_limit', 5)):>7} {s.get('channel','')}  (last: {when})")
    return 0


def cmd_config(sid, key=None, val=None):
    s = load_one(sid)
    if not s:
        print(f"No RedNote stream '{sid}'.")
        return 1
    if not key:
        print(json.dumps(s, indent=2, ensure_ascii=False))
        return 0
    if val is None:
        print(f"{key} = {s.get(key)}")
        return 0
    if key in ("tags",):
        v = _split_csv(val)
    elif key in ("limit", "detail_limit"):
        v = int(val)
    else:
        v = val
    s[key] = v
    save_one(s)
    print(f"{sid}.{key} = {v}")
    return 0


def _resolve(ref):
    s = load_one(ref)
    if s:
        return s
    matches = [x for x in load_all() if ref.lower() in x.get("query", "").lower()]
    return matches[0] if len(matches) == 1 else None


def cmd_preview(ref):
    s = _resolve(ref)
    if not s:
        print(f"No RedNote stream matches '{ref}'.")
        return 1
    print(f"── REDNOTE PREVIEW: {s['id']} ─────────────────────────")
    print(f"query: {s['query']}  ·  limit: {s.get('limit', 20)}  ·  detail_limit: {s.get('detail_limit', 5)}")
    print("collecting via OpenCLI rednote adapter…\n")
    try:
        payload = collect(s)
    except Exception as e:
        print(f"OpenCLI collection failed: {e}")
        print("\nRun `wechat-daily rednote check`, then make sure Chrome is open, logged into rednote.com, and Browser Bridge is connected.")
        return 1
    try:
        intel, ok = run_agent(s, payload)
    except Exception as e:
        print(f"Claude analysis failed: {e}")
        return 1
    print(intel if ok else "(agent produced no output)")
    print("\n───────────────────────────────────────────────────────")
    print(f"Publish for real: wechat-daily rednote publish {s['id']}")
    return 0


def publish_stream(s, verbose=True):
    if s.get("status") != "active":
        if verbose:
            print(f"  {s['id']}: paused")
        return 0
    if verbose:
        print(f"  {s['id']}: collecting RedNote results via OpenCLI…")
    payload = collect(s)
    if verbose:
        print(f"  {s['id']}: analyzing {len(payload['search_results'])} results via claude ({s.get('agent_model','sonnet')})…")
    intel, ok = run_agent(s, payload)
    if not ok or not intel:
        if verbose:
            print(f"  ✗ {s['id']}: agent produced nothing")
        return 0
    stamp = datetime.now().strftime("%Y-%m-%d")
    summary = f"{s['name']} · RedNote · {stamp} · intel"
    oneliner = ("RedNote " + stamp[5:])[:15]
    ok2, eid, msg = st.router_write(summary, intel, s.get("channel"),
                                    s.get("tags", DEFAULT_TAGS), oneliner, [])
    if ok2:
        s["last_published_at"] = int(time.time())
        save_one(s)
        if verbose:
            print(f"  ✓ {s['id']}: intel update → {s.get('channel')} [{eid}]")
        return 1
    if verbose:
        print(f"  ✗ {s['id']}: publish failed — {msg[:120]}")
    return 0


def cmd_publish(ref):
    s = _resolve(ref)
    if not s:
        print(f"No RedNote stream matches '{ref}'.")
        return 1
    try:
        n = publish_stream(s, verbose=True)
    except Exception as e:
        print(f"RedNote publish failed: {e}")
        print("Run `wechat-daily rednote check`, then retry preview before publishing.")
        return 1
    print(f"Published {n} RedNote entr{'y' if n == 1 else 'ies'} for '{s['id']}'.")
    return 0


def publish_due(verbose=True):
    rows = [s for s in load_all() if s.get("status") == "active"]
    if not rows:
        return
    if verbose:
        print(f"\nrednote: publishing {len(rows)} active …")
    for s in rows:
        try:
            publish_stream(s, verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"  ✗ {s.get('id')}: {e}")


def dispatch(args):
    a = args.rednote_action or "list"
    rest = list(args.rednote_rest)
    if a == "check":
        return cmd_check()
    if a in ("list", "status"):
        return cmd_list()
    if a == "add":
        return cmd_add(" ".join(rest))
    if a == "config":
        sid = rest[0] if rest else ""
        key = rest[1] if len(rest) > 1 else None
        val = " ".join(rest[2:]) if len(rest) > 2 else None
        return cmd_config(sid, key, val)
    if a == "preview":
        return cmd_preview(" ".join(rest))
    if a == "publish":
        return cmd_publish(" ".join(rest))
    print(f"unknown rednote action '{a}'")
    return 1
