#!/usr/bin/env python3
"""crew_tokens: count where Claude Code tokens went, from the saved chats on this Mac.

  crew_tokens.py report [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--out FILE.md] [--label] [--json FILE.json]
  crew_tokens.py pace   (weekly reset: env CREW_WEEK_START, e.g. thu03)
  crew_tokens.py days   --from ... --to ... --json FILE.json     (daily numbers for the flight log)

Reads ~/.claude/projects/**.jsonl only. Changes nothing. Makes no AI calls unless --label is given:
then Haiku (through `claude -p`, the user's own plan) reads only the user's own short messages, with
pasted text, code and anything key-like removed, to count corrections. Results are cached.

"Price-equivalent" uses list API prices. It is NOT the weekly usage bar; it only shows proportions.
"""
import datetime as dt
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict

PROJECTS = os.environ.get("CREW_PROJECTS") or os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")

# $ per million tokens (input, output), checked 2026-11-11. Cache: 5-min write 1.25x input,
# 1-hour write 2x input, read 0.1x input.
PRICES = {"fable": (8.0, 40.0), "opus": (4.0, 20.0), "sonnet": (2.0, 10.0), "haiku": (0.10, 0.50)}
FAMILIES = ("fable", "opus", "sonnet", "haiku")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_week_start(v):
    """'thu03' -> (3, 3): weekday (Mon=0) and local hour of the weekly usage reset."""
    v = (v or "").strip().lower()
    try:
        return DAYS.index(v[:3]), int(v[3:] or 0) % 24
    except ValueError:
        return 3, 3


# The weekly usage reset, local time. Set CREW_WEEK_START (e.g. "mon09") to match yours; default Thursday 03:00.
WEEK_START = parse_week_start(os.environ.get("CREW_WEEK_START", "thu03"))
SKIP_CWD = ("/scratchpad",)  # Claude's own throwaway test folders


def family(model):
    m = (model or "").lower()
    for f in FAMILIES:
        if f in m and "deepseek" not in m:
            return f
    return "other"


def price(fam, inp, w5, w1, read, out):
    if fam not in PRICES:
        return 0.0
    pi, po = PRICES[fam]
    return (inp * pi + w5 * pi * 1.25 + w1 * pi * 2 + read * pi * 0.1 + out * po) / 1e6


def local(ts):
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def project_of(cwd):
    if not cwd:
        return "?"
    home = os.path.expanduser("~")
    for marker in ("/VS Code Projects/", "/Documents/", "/Projects/"):
        if marker in cwd:
            return cwd.split(marker, 1)[1].split("/")[0] or "?"
    if cwd.rstrip("/") == home:
        return "~ (home)"
    return os.path.basename(cwd.rstrip("/")) or cwd


SYSTEM_PREFIXES = ("<local-command", "<command-message", "Caveat:", "<system-reminder", "<task-notification",
                   "This session is being continued", "[Request interrupted", "<bash-input", "<bash-stdout",
                   "<bash-stderr", "<user-memory", "<persisted-output", "<channel")
TICKET_RE = re.compile(r"(\btickets?/|\bticket\s*#?\d|\bticket\b.*\b(build|implement|start|continue|next|do)\b|"
                       r"\b(build|implement|start|continue|do|next)\b.*\bticket\b)", re.I)
HANDOFF_RE = re.compile(r"continue from handoff", re.I)
CMD_RE = re.compile(r"<command-name>/?([^<]+)</command-name>")
ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def user_text(m):
    """The user's own typed text, or None for tool results, hook output and system lines."""
    if m.get("isMeta") or m.get("isCompactSummary") or m.get("isVisibleInTranscriptOnly"):
        return None
    c = (m.get("message") or {}).get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        if not parts or any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        text = "\n".join(parts)
    else:
        return None
    t = text.strip()
    if not t:
        return None
    if "<command-name>" in t[:300]:
        return t  # a slash command the user typed; kept for /boost and /handoff counts
    if t.startswith(SYSTEM_PREFIXES):
        return None
    return t


def norm_cmd(tool, inp):
    if tool == "Bash":
        s = (inp or {}).get("command", "")
    else:
        s = json.dumps(inp, sort_keys=True)[:200] if inp else ""
    s = re.sub(r"\s+", " ", s).strip()
    return f"{tool}:{s[:120]}"


class Data:
    def __init__(self):
        self.replies = []  # one per API message
        self.advisor = []  # one per advisor call
        self.sessions = {}  # main session id -> info
        self.files = 0
        self.seen = set()


def session(d, sid):
    if sid not in d.sessions:
        d.sessions[sid] = {"id": sid, "project": "?", "start": None, "end": None, "user": [], "commands": [],
                           "replies": 0, "fails": defaultdict(int), "models": defaultdict(float),
                           "price": 0.0, "kind": "main", "entrypoint": None, "skip": False}
    return d.sessions[sid]


def read_file(d, path, start, end, helper_meta=None):
    sid_from_path = None
    is_helper = "/subagents/" in path
    if is_helper:
        sid_from_path = path.split("/subagents/")[0].rsplit("/", 1)[-1]
    else:
        sid_from_path = os.path.basename(path)[:-6]
    kind = "main"
    agent_type = None
    if is_helper:
        kind = "helper"
        meta = helper_meta or {}
        agent_type = meta.get("agentType")
        name = (meta.get("name") or "") + " " + (meta.get("description") or "")
        if re.search(r"\bticket[- ]?\d|\bticket\b", name, re.I):
            kind = "ticket"
    pending_tools = {}
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    d.files += 1
    entries = []
    final = {}  # message id -> the last saved version that has usage (later lines can carry updated counts)
    with f:
        for line in f:
            if line.startswith('{"type":"api-request') or line.startswith('{"type":"queue-operation'):
                continue
            if '"assistant"' not in line and '"user"' not in line:
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            entries.append(m)
            if m.get("type") == "assistant":
                msg = m.get("message") or {}
                if msg.get("usage"):
                    final[msg.get("id") or m.get("requestId") or m.get("uuid")] = m
    if True:
        for m in entries:
            t = m.get("type")
            if t not in ("assistant", "user"):
                continue
            when = local(m.get("timestamp"))
            if when is None or when.date() < start or when.date() > end:
                continue
            cwd = m.get("cwd") or ""
            if any(s in cwd for s in SKIP_CWD):
                continue
            sid = m.get("sessionId") or sid_from_path
            s = session(d, sid)
            if not is_helper:
                s["project"] = project_of(cwd) if cwd else s["project"]
                s["entrypoint"] = s["entrypoint"] or m.get("entrypoint")
                s["start"] = min(s["start"] or when, when)
                s["end"] = max(s["end"] or when, when)
            elif s["project"] == "?" and cwd:
                s["project"] = project_of(cwd)
            msg = m.get("message") or {}
            if t == "user":
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                            key = pending_tools.get(b.get("tool_use_id"))
                            if key:
                                s["fails"][key] += 1
                if not is_helper:
                    text = user_text(m)
                    if text:
                        cm = CMD_RE.search(text) if "<command-name>" in text[:300] else None
                        if cm:
                            am = ARGS_RE.search(text)
                            s["commands"].append((when, (cm.group(1).strip() + " " + (am.group(1).strip() if am else "")).strip()))
                        else:
                            s["user"].append((when, text))
                continue
            # assistant
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    pending_tools[b.get("id")] = norm_cmd(b.get("name"), b.get("input"))
            mid = msg.get("id") or m.get("requestId") or m.get("uuid")
            if mid in d.seen or mid not in final:
                continue
            d.seen.add(mid)
            m = final[mid]
            msg = m.get("message") or {}
            u = msg.get("usage") or {}
            model = msg.get("model") or ""
            if model == "<synthetic>" or not u:
                continue
            cc = u.get("cache_creation") or {}
            w1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
            w5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
            wtot = u.get("cache_creation_input_tokens", 0) or 0
            if w1 + w5 < wtot:
                w5 += wtot - w1 - w5
            fam = family(model)
            # Who set the model: the main chat itself, or for a helper whether its starter named a model.
            picked = "main" if not is_helper else ("asked" if (helper_meta or {}).get("model") else "inherited")
            r = {"when": when, "session": sid, "project": s["project"], "kind": kind, "agent": agent_type, "picked": picked,
                 "model": model, "family": fam, "effort": m.get("effort"),
                 "input": u.get("input_tokens", 0) or 0, "w5": w5, "w1": w1,
                 "read": u.get("cache_read_input_tokens", 0) or 0, "output": u.get("output_tokens", 0) or 0}
            r["price"] = price(fam, r["input"], w5, w1, r["read"], r["output"])
            d.replies.append(r)
            s["replies"] += 1
            s["models"][fam] += r["price"]
            s["price"] += r["price"]
            for it in u.get("iterations") or []:
                if it.get("type") == "advisor_message":
                    am = it.get("model") or m.get("advisorModel") or "fable"
                    af = family(am)
                    icc = it.get("cache_creation") or {}
                    a = {"when": when, "session": sid, "project": s["project"], "kind": kind, "model": am,
                         "family": af, "input": it.get("input_tokens", 0) or 0,
                         "read": it.get("cache_read_input_tokens", 0) or 0,
                         "w5": icc.get("ephemeral_5m_input_tokens", 0) or 0,
                         "w1": icc.get("ephemeral_1h_input_tokens", 0) or 0,
                         "output": it.get("output_tokens", 0) or 0}
                    a["price"] = price(af, a["input"], a["w5"], a["w1"], a["read"], a["output"])
                    d.advisor.append(a)
                    s["price"] += a["price"]
                    s["models"]["advisor:" + af] += a["price"]


def collect(start, end):
    d = Data()
    lo = dt.datetime.combine(start, dt.time()).timestamp() - 86400
    for path in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        if os.path.getmtime(path) < lo:
            continue
        read_file(d, path, start, end)
    for path in glob.glob(os.path.join(PROJECTS, "*", "*", "subagents", "*.jsonl")):
        if os.path.getmtime(path) < lo:
            continue
        meta = {}
        mp = path[:-6] + ".meta.json"
        if os.path.exists(mp):
            try:
                meta = json.load(open(mp, encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        read_file(d, path, start, end, helper_meta=meta)
    # Main chats that are ticket builds by hand.
    for s in d.sessions.values():
        first = " ".join(re.sub(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", " ", t, flags=re.S)
                         for _, t in s["user"][:2])
        if TICKET_RE.search(first) or any(c.split()[0].lower() in ("run-tickets", "/run-tickets")
                                           for _, c in s["commands"] if c):
            s["kind"] = "ticket"
    for r in d.replies:
        if r["kind"] == "main" and d.sessions.get(r["session"], {}).get("kind") == "ticket":
            r["kind"] = "ticket-main"
    for a in d.advisor:
        if a["kind"] == "main" and d.sessions.get(a["session"], {}).get("kind") == "ticket":
            a["kind"] = "ticket-main"
    return d


# ---------- the user's messages for Haiku ----------

KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|xox[a-z]-[A-Za-z0-9\-]{8,}|"
                     r"eyJ[A-Za-z0-9_\-]{20,}|\b[0-9a-f]{32,}\b|"
                     r"\b(?=[A-Za-z0-9_+/=]*[A-Z])(?=[A-Za-z0-9_+/=]*[a-z])(?=[A-Za-z0-9_+/=]*[0-9])[A-Za-z0-9_+/=]{24,})")


def clean_for_haiku(text):
    t = re.sub(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", " [pasted text] ", text, flags=re.S)
    t = re.sub(r"```.*?```", " [code] ", t, flags=re.S)
    t = re.sub(r"`[^`\n]{40,}`", " [code] ", t)
    t = KEYLIKE.sub("[removed]", t)
    t = re.sub(r"(/Users/|~/)[^\s)\]]+", "[path]", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:280]


HAIKU_PROMPT = """You label a person's messages to an AI coding assistant. You see ONLY the person's own messages,
in order, for several chats. For each chat, count:
- "corrections": messages where the person tells the assistant it went the wrong way, misunderstood, made a mistake,
  or that something it did does not work / is still broken. A new request, a follow-up task, "yes"/"go"/"thanks",
  or adding detail is NOT a correction.
- "reopen": true only if the FIRST message says an earlier job is still broken, came back, or didn't work.
Reply with JSON only, exactly: {"<chat id>": {"corrections": <int>, "reopen": <true|false>}, ...} with every chat id.

Chats:
"""


def haiku_label(chats, cache_path, log):
    """chats: {sid: [texts]}. Returns {sid: {"corrections": n, "reopen": b}}. Uses and fills the cache."""
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    out, todo = {}, {}
    for sid, msgs in chats.items():
        h = hashlib.sha256(json.dumps(msgs).encode()).hexdigest()[:16]
        key = f"{sid}:{h}"
        if key in cache:
            out[sid] = cache[key]
        else:
            todo[sid] = (key, msgs)
    items = list(todo.items())
    batch, size = [], 0
    batches = []
    for sid, (key, msgs) in items:
        block = len(json.dumps(msgs))
        if batch and (size + block > 12000 or len(batch) >= 25):
            batches.append(batch)
            batch, size = [], 0
        batch.append((sid, key, msgs))
        size += block
    if batch:
        batches.append(batch)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_EFFORT",
                        "CLAUDE_PLUGIN_DATA", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")}
    workdir = tempfile.mkdtemp(prefix="crew-label-")
    for b in batches:
        payload = {f"c{i}": [f"{j + 1}. {m}" for j, m in enumerate(msgs)] for i, (_, _, msgs) in enumerate(b)}
        prompt = HAIKU_PROMPT + json.dumps(payload, ensure_ascii=False, indent=0)
        try:
            r = subprocess.run(["claude", "-p", prompt, "--model", "haiku", "--no-session-persistence",
                                "--output-format", "json", "--tools", "", "--strict-mcp-config",
                                "--setting-sources", "project",
                                "--system-prompt", "You label chat messages. Reply only with JSON."],
                               capture_output=True, text=True, timeout=180, cwd=workdir, env=env)
            res = json.loads(r.stdout)
        except (subprocess.SubprocessError, ValueError, OSError) as e:
            log["errors"] += 1
            continue
        log["calls"] += 1
        log["cost"] += res.get("total_cost_usd") or 0
        for mu in (res.get("modelUsage") or {}).values():
            log["tokens"] += (mu.get("inputTokens", 0) + mu.get("outputTokens", 0)
                              + mu.get("cacheReadInputTokens", 0) + mu.get("cacheCreationInputTokens", 0))
        text = res.get("result") or ""
        mjson = re.search(r"\{.*\}", text, re.S)
        try:
            labels = json.loads(mjson.group(0)) if mjson else {}
        except ValueError:
            labels = {}
            log["errors"] += 1
        for i, (sid, key, _) in enumerate(b):
            lab = labels.get(f"c{i}")
            if isinstance(lab, dict):
                v = {"corrections": int(lab.get("corrections", 0) or 0), "reopen": bool(lab.get("reopen"))}
                out[sid] = v
                cache[key] = v
    if cache_path:
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    return out


# ---------- describing chats ----------

def repeat_fails(s):
    return sum(1 for n in s["fails"].values() if n >= 2)


def describe(s, lab):
    """Struggled / Smooth / Short and clean, per the spec. None if unlabelled and undecidable."""
    n_user = len(s["user"])
    rf = repeat_fails(s)
    corr = lab.get("corrections") if lab else None
    reopen = lab.get("reopen") if lab else False
    if corr is None:
        if rf:
            return "Struggled"
        return None
    if corr >= 2 or rf or reopen:
        return "Struggled"
    if corr == 0 and n_user <= 3 and s["replies"] <= 30:
        return "Short and clean"
    return "Smooth"


# ---------- weeks and pace ----------

def week_start(when):
    """Start of the usage week (local wall-clock time, so clock changes don't split a week)."""
    wd, hour = WEEK_START
    when = when.replace(tzinfo=None)
    d0 = when.replace(hour=hour, minute=0, second=0, microsecond=0)
    back = (when.weekday() - wd) % 7
    d0 = d0 - dt.timedelta(days=back)
    if d0 > when:
        d0 -= dt.timedelta(days=7)
    return d0


def pace(replies, advisor, now=None):
    """Normal share of a week's price-equivalent used by the end of each day of the week (1..7)."""
    weeks = defaultdict(lambda: [0.0] * 7)
    first = None
    for r in list(replies) + list(advisor):
        w = r["when"].replace(tzinfo=None)
        first = min(first or w, w)
        ws = week_start(w)
        day = min(6, int((w - ws).total_seconds() // 86400))
        weeks[ws][day] += r["price"]
    now = (now or dt.datetime.now()).replace(tzinfo=None)
    full = {ws: v for ws, v in weeks.items()
            if ws + dt.timedelta(days=7) <= now and sum(v) > 0 and first and ws >= first - dt.timedelta(hours=1)}
    rows = []
    for ws, v in sorted(full.items()):
        tot = sum(v)
        cum, acc = [], 0.0
        for x in v:
            acc += x
            cum.append(acc / tot)
        rows.append((ws, tot, cum))
    normal = [sum(r[2][i] for r in rows) / len(rows) for i in range(7)] if rows else None
    return rows, normal, weeks


# ---------- report ----------

def fmt_tok(n):
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return str(int(n))


def money(x):
    return f"${x:,.2f}"


FABLE_ROWS = (
    ("main", "Main chat on Fable", "You picked Fable for the chat (/model, or a Fable handoff)"),
    ("asked", "Helper started on Fable", "The chat named Fable when it started the helper (sometimes because you asked)"),
    ("inherited", "Helper on Fable, no model named", "It took the chat's model, or its helper file names Fable"),
    ("advisor-main", "Advisor, main chat", "Claude called the advisor in a chat with you"),
    ("advisor-helper", "Advisor, helpers and ticket builds", "A helper called the advisor on its own"),
)


def fable_split(d, since=None):
    """Fable spend by who set it. Returns {row key: [count, price]}; the rows add up to all Fable spend."""
    out = {k: [0, 0.0] for k, _, _ in FABLE_ROWS}
    for r in d.replies:
        if r["family"] == "fable" and not (since and r["when"] < since):
            k = r.get("picked") or "main"
            out[k][0] += 1
            out[k][1] += r["price"]
    for a in d.advisor:
        if a["family"] == "fable" and not (since and a["when"] < since):
            k = "advisor-main" if a["kind"] in ("main", "ticket-main") else "advisor-helper"
            out[k][0] += 1
            out[k][1] += a["price"]
    return out


def report(start, end, out_path=None, label=False, json_path=None, cache_path=None):
    d = collect(start, end)
    sessions = {k: s for k, s in d.sessions.items() if s["start"] is not None or s["replies"]}
    lines = []
    total = sum(r["price"] for r in d.replies) + sum(a["price"] for a in d.advisor)
    first = min((r["when"] for r in d.replies), default=None)
    last = max((r["when"] for r in d.replies), default=None)
    lines.append(f"# Before-table: where the tokens went, {start} to {end}")
    lines.append("")
    if first:
        lines.append(f"Saved chats cover **{first:%Y-%m-%d} to {last:%Y-%m-%d}** inside the range asked "
                     f"({(last.date() - first.date()).days + 1} days). Claude Code keeps saved chats 30 days by default, "
                     "so older days are gone.")
    lines.append(f"Read {d.files} files: {sum(1 for s in sessions.values() if s['start'])} main chats, "
                 f"{len(d.replies)} replies, {len(d.advisor)} advisor calls. Claude's own test folders are left out.")
    lines.append("")
    lines.append("> **Price-equivalent** = what these tokens would cost at list API prices. It is **not** the weekly "
                 "usage bar (your plan doesn't charge per token, and how the limit weighs each model isn't published). "
                 "Use it for proportions only.")
    lines.append("")

    # by model
    lines.append("## By model")
    lines.append("")
    lines.append("| Model | Replies | Fresh input | Cache read | Output | Price-equivalent | Share |")
    lines.append("|---|---|---|---|---|---|---|")
    agg = defaultdict(lambda: [0, 0, 0, 0, 0.0])
    for r in d.replies:
        a = agg[r["family"]]
        a[0] += 1
        a[1] += r["input"] + r["w5"] + r["w1"]
        a[2] += r["read"]
        a[3] += r["output"]
        a[4] += r["price"]
    for x in d.advisor:
        a = agg["advisor: " + x["family"]]
        a[0] += 1
        a[1] += x["input"] + x["w5"] + x["w1"]
        a[2] += x["read"]
        a[3] += x["output"]
        a[4] += x["price"]
    for k, a in sorted(agg.items(), key=lambda kv: -kv[1][4]):
        name = {"other": "other (not Claude, no price)"}.get(k, k.capitalize() if ":" not in k else k)
        lines.append(f"| {name} | {a[0]} | {fmt_tok(a[1])} | {fmt_tok(a[2])} | {fmt_tok(a[3])} | {money(a[4])} | "
                     f"{100 * a[4] / total:.1f}% |" if total else f"| {name} | {a[0]} | | | | | |")
    lines.append("")

    # by kind
    lines.append("## By kind of work")
    lines.append("")
    kinds = defaultdict(lambda: defaultdict(float))
    kind_name = {"main": "Main chat", "helper": "Helper", "ticket": "Ticket build (helper)",
                 "ticket-main": "Ticket build (main chat)"}
    for r in d.replies:
        kinds[r["kind"]][r["family"]] += r["price"]
    for x in d.advisor:
        kinds[x["kind"]]["advisor"] += x["price"]
    lines.append("| Kind | Price-equivalent | Share | Opus | Fable | Sonnet | Haiku | Advisor |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for k in ("main", "ticket-main", "ticket", "helper"):
        v = kinds.get(k)
        if not v:
            continue
        tot = sum(v.values())
        cells = " | ".join(money(v.get(f, 0)) for f in ("opus", "fable", "sonnet", "haiku", "advisor"))
        lines.append(f"| {kind_name[k]} | {money(tot)} | {100 * tot / total:.1f}% | {cells} |")
    helper_types = defaultdict(float)
    for r in d.replies:
        if r["kind"] in ("helper", "ticket"):
            helper_types[r["agent"] or "unknown"] += r["price"]
    if helper_types:
        top = sorted(helper_types.items(), key=lambda kv: -kv[1])[:6]
        lines.append("")
        lines.append("Helpers by type: " + ", ".join(f"{k} {money(v)}" for k, v in top) + ".")
    ticket_share = sum(sum(kinds.get(k, {}).values()) for k in ("ticket", "ticket-main")) / total if total else 0
    lines.append("")

    # who picked Fable
    split = fable_split(d)
    ftot = sum(v[1] for v in split.values())
    lines.append("## Who picked Fable")
    lines.append("")
    if ftot:
        lines.append("| Where Fable ran | What it means | Replies or calls | Price-equivalent | Share of Fable |")
        lines.append("|---|---|---|---|---|")
        for k, name, meaning in FABLE_ROWS:
            n, p = split[k]
            if n:
                lines.append(f"| {name} | {meaning} | {n} | {money(p)} | {100 * p / ftot:.0f}% |")
        lines.append(f"| **All Fable** | | {sum(v[0] for v in split.values())} | **{money(ftot)}** | 100% |")
        lines.append("")
        lines.append("The saved chats show which chat started a helper on Fable, not whether you asked it to.")
    else:
        lines.append("No Fable in this range.")
    lines.append("")

    # by project
    lines.append("## By project (top 15)")
    lines.append("")
    lines.append("| Project | Chats | Price-equivalent | Share | Main model mix |")
    lines.append("|---|---|---|---|---|")
    proj = defaultdict(lambda: [set(), 0.0, defaultdict(float)])
    for r in d.replies:
        p = proj[r["project"]]
        p[0].add(r["session"])
        p[1] += r["price"]
        p[2][r["family"]] += r["price"]
    for x in d.advisor:
        p = proj[x["project"]]
        p[1] += x["price"]
        p[2]["advisor"] += x["price"]
    for k, p in sorted(proj.items(), key=lambda kv: -kv[1][1])[:15]:
        mix = ", ".join(f"{f} {100 * v / p[1]:.0f}%" for f, v in sorted(p[2].items(), key=lambda kv: -kv[1]) if p[1] and v / p[1] >= 0.02)
        lines.append(f"| {k} | {len(p[0])} | {money(p[1])} | {100 * p[1] / total:.1f}% | {mix} |")
    lines.append("")

    # pace
    range_end = dt.datetime.combine(end + dt.timedelta(days=1), dt.time())
    rows, normal, weeks = pace(d.replies, d.advisor, now=min(range_end, dt.datetime.now()))
    lines.append("## Normal pace by day of the week")
    lines.append("")
    lines.append(f"Weeks run from the weekly reset, **{DAYS[WEEK_START[0]].title()} {WEEK_START[1]:02d}:00**. Share of the week's price-equivalent used by the "
                 "end of each day. Only full weeks count.")
    lines.append("")
    if rows:
        hdr = ["Week from"] + [f"Day {i + 1} ({(rows[0][0] + dt.timedelta(days=i)):%a})" for i in range(7)] + ["Week total"]
        lines.append("| " + " | ".join(hdr) + " |")
        lines.append("|" + "---|" * len(hdr))
        for ws, tot, cum in rows:
            lines.append(f"| {ws:%Y-%m-%d} | " + " | ".join(f"{100 * c:.0f}%" for c in cum) + f" | {money(tot)} |")
        lines.append("| **Normal (average)** | " + " | ".join(f"**{100 * c:.0f}%**" for c in normal) + " | |")
        lines.append("")
        lines.append("A flat week would be 14%, 29%, 43%, 57%, 71%, 86%, 100%.")
    else:
        lines.append("No full week in the range yet.")
    lines.append("")

    # chats
    log = {"calls": 0, "cost": 0.0, "tokens": 0, "errors": 0}
    labels = {}
    if label:
        chats = {}
        for sid, s in sessions.items():
            texts = [clean_for_haiku(t) for _, t in s["user"]]
            texts = [t for t in texts if t]
            if texts:
                chats[sid] = texts[:40]
        labels = haiku_label(chats, cache_path, log)
    lines.append("## How the chats went")
    lines.append("")
    lines.append("Struggled: corrected twice, the same command failed twice, or the first message reopens an earlier job. "
                 "Smooth: at most one correction, nothing failed twice. Short and clean: three messages or fewer, no "
                 "corrections. These describe how the job went, not whether the model was to blame.")
    lines.append("")
    desc = defaultdict(int)
    by_proj_desc = defaultdict(lambda: defaultdict(int))
    mains = [s for s in sessions.values() if s["start"] and s["user"]]
    for s in mains:
        k = describe(s, labels.get(s["id"])) or "Not labelled"
        s["desc"] = k
        desc[k] += 1
        by_proj_desc[s["project"]][k] += 1
    lines.append("| Struggled | Smooth | Short and clean | Not labelled |")
    lines.append("|---|---|---|---|")
    lines.append(f"| {desc['Struggled']} | {desc['Smooth']} | {desc['Short and clean']} | {desc['Not labelled']} |")
    if not label:
        lines.append("")
        lines.append("Corrections weren't counted (run with `--label`), so only repeated failures mark a chat Struggled.")
    lines.append("")
    lines.append("By project (top 10 by chats):")
    lines.append("")
    lines.append("| Project | Struggled | Smooth | Short and clean |")
    lines.append("|---|---|---|---|")
    for p, v in sorted(by_proj_desc.items(), key=lambda kv: -sum(kv[1].values()))[:10]:
        lines.append(f"| {p} | {v['Struggled']} | {v['Smooth']} | {v['Short and clean']} |")
    lines.append("")

    # loops
    lines.append("## Likely loops to check against your memory")
    lines.append("")
    lines.append("Chats with commands that failed again and again, longest first. Tick the ones that really went in circles.")
    lines.append("")
    cand = sorted(mains, key=lambda s: (-repeat_fails(s), -sum(s["fails"].values()), -len(s["user"])))
    cand = [s for s in cand if repeat_fails(s) >= 2 or sum(s["fails"].values()) >= 6][:12]
    if cand:
        lines.append("| Date | Project | Your messages | Failed commands (repeats) | Corrections | First message |")
        lines.append("|---|---|---|---|---|---|")
        for s in cand:
            lab = labels.get(s["id"]) or {}
            firstmsg = clean_for_haiku(s["user"][0][1])[:90].replace("|", "/") if s["user"] else ""
            lines.append(f"| {s['start']:%Y-%m-%d} | {s['project']} | {len(s['user'])} | "
                         f"{sum(s['fails'].values())} ({repeat_fails(s)}) | {lab.get('corrections', '?')} | {firstmsg} |")
    else:
        lines.append("None found.")
    lines.append("")

    # verdict
    lines.append("## Does ticket building take a big share?")
    lines.append("")
    strict = sum(kinds.get("ticket", {}).values()) / total if total else 0
    lines.append(f"Ticket builds are **{100 * ticket_share:.0f}%** of the price-equivalent: "
                 f"{100 * strict:.0f}% from helpers started for a ticket (sure), plus "
                 f"{100 * (ticket_share - strict):.0f}% from chats whose first two messages are about building a ticket "
                 "(a guess from the wording). "
                 + ("That is a big share (25% or more): moving well-scoped ticket builds to Sonnet is worth trying in week two."
                    if ticket_share >= 0.25 else
                    "That is borderline (15% to 25%): Sonnet for ticket builds would save about half of it, so roughly "
                    f"{50 * ticket_share:.0f}% of the week at most. Worth it only if week one doesn't fix the pace another way."
                    if ticket_share >= 0.15 else
                    "That is not a big share (under 15%): Sonnet for ticket builds would save little."))
    lines.append("")
    lines.append("## Cost of making this table")
    lines.append("")
    if label:
        if log["calls"] == 0 and not log["errors"]:
            lines.append("Haiku labels all came from the cache of an earlier run: no new AI calls, $0.")
        else:
            lines.append(f"Haiku labelling: {log['calls']} calls, {fmt_tok(log['tokens'])} tokens, {money(log['cost'])} "
                       f"price-equivalent{', ' + str(log['errors']) + ' failed calls' if log['errors'] else ''}. "
                       "Haiku saw only your own messages, shortened, with pasted text, code, paths and key-like strings removed.")
    else:
        lines.append("No AI calls: $0.")
    text = "\n".join(lines) + "\n"
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    if json_path:
        dump_json(d, json_path, labels)
    return text, d, log, ticket_share


def daily(d):
    days = defaultdict(lambda: defaultdict(float))
    for r in d.replies:
        days[r["when"].date().isoformat()][r["family"]] += r["price"]
        days[r["when"].date().isoformat()]["tok:" + r["family"]] += r["input"] + r["w5"] + r["w1"] + r["read"] + r["output"]
    for a in d.advisor:
        days[a["when"].date().isoformat()]["advisor"] += a["price"]
        days[a["when"].date().isoformat()]["tok:advisor"] += a["input"] + a["w5"] + a["w1"] + a["read"] + a["output"]
    events = []
    for a in d.advisor:
        events.append({"when": a["when"].isoformat(), "type": "advisor", "price": round(a["price"], 4)})
    for s in d.sessions.values():
        for when, cmd in s["commands"]:
            parts = cmd.lower().split()
            name, arg = parts[0], (parts[1] if len(parts) > 1 else "")
            if name in ("boost", "crew:boost") or (name in ("crew", "crew:crew") and arg == "boost"):
                events.append({"when": when.isoformat(), "type": "boost"})
            elif name == "handoff":
                events.append({"when": when.isoformat(), "type": "handoff"})
        # A Fable handoff: a chat that continues from a handoff and runs mostly on Fable.
        if s["user"] and HANDOFF_RE.search(s["user"][0][1]) and s["models"].get("fable", 0) > 0.5 * max(s["price"], 1e-9):
            events.append({"when": s["user"][0][0].isoformat(), "type": "fable-handoff"})
    return {k: dict(v) for k, v in sorted(days.items())}, sorted(events, key=lambda e: e["when"])


def dump_json(d, path, labels=None):
    days, events = daily(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"days": days, "events": events}, f, indent=1)


def parse_args(argv):
    opts = {"from": None, "to": None, "out": None, "json": None, "label": False, "cache": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--from", "--to", "--out", "--json", "--cache"):
            opts[a[2:]] = argv[i + 1]
            i += 2
        elif a == "--label":
            opts["label"] = True
            i += 1
        else:
            i += 1
    today = dt.date.today()
    opts["to"] = dt.date.fromisoformat(opts["to"]) if opts["to"] else today
    opts["from"] = dt.date.fromisoformat(opts["from"]) if opts["from"] else opts["to"] - dt.timedelta(days=27)
    return opts


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, o = argv[0], parse_args(argv[1:])
    if cmd == "report":
        text, d, log, share = report(o["from"], o["to"], o["out"], o["label"], o["json"], o["cache"])
        if not o["out"]:
            print(text)
        else:
            print(f"Saved {o['out']}")
        return 0
    if cmd == "days":
        d = collect(o["from"], o["to"])
        if o["json"]:
            dump_json(d, o["json"])
            print(f"Saved {o['json']}")
        else:
            print(json.dumps(daily(d)[0], indent=1))
        return 0
    if cmd == "pace":
        d = collect(o["to"] - dt.timedelta(days=35), o["to"])
        rows, normal, _ = pace(d.replies, d.advisor)
        print("normal:", [round(100 * x) for x in normal] if normal else None)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
