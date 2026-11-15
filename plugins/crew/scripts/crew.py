#!/usr/bin/env python3
"""crew: switch Claude Code to the crew setup and back.

  crew.py takeoff --data DIR   turn crew on (Opus, medium effort for Opus, Fable advisor)
  crew.py land    --data DIR   put back exactly what takeoff changed
  crew.py status  --data DIR   say whether crew is on, and show the three values
  crew.py log     --data DIR   build the flight log page and print where it is
  crew.py keep    --data DIR <folder>   also save landing cards and the flight log in <folder>
  crew.py check                read-only leftover check (works without the plugin)

Takeoff writes a receipt in crew's data folder BEFORE it changes anything.
A backup with manual undo steps lives in ~/.claude/crew-receipt.json and the keep folder.
Land can recover from that backup if plugin data is gone, then removes crew's receipts.

It changes only these values in ~/.claude/settings.json:
  model, advisorModel, modelSettings -> claude-opus-5-5 -> effortLevel
and creates ~/.claude/agents/Explore.md (marked "made by crew" on line 2).
Everything else in the settings file is left byte for byte as it was.

Standard library only. Set CREW_HOME to point at another .claude folder (tests).
"""
import datetime
import hashlib
import json
import os
import sys
import tempfile

HOME = os.environ.get("CREW_HOME") or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
SETTINGS = os.path.join(HOME, "settings.json")
AGENTS = os.path.join(HOME, "agents")
EXPLORE = os.path.join(AGENTS, "Explore.md")
BACKUP = os.path.join(HOME, "crew-receipt.json")
MARKER = "made by crew"
NOT_SET = "<not set>"
OPUS_ID = "claude-opus-5-5"

# The three values crew sets. Path inside settings.json -> value takeoff sets.
TARGETS = [
    (("model",), "opus"),
    (("modelSettings", OPUS_ID, "effortLevel"), "medium"),
    (("advisorModel",), "fable"),
]
LABELS = {
    "model": "main model",
    "modelSettings." + OPUS_ID + ".effortLevel": "Opus effort",
    "advisorModel": "advisor",
}
NEW_CHAT = ("Chats already open keep their old model and effort (the advisor switches in them too). "
            "Start a new chat.")

EXPLORE_TEXT = """---
# made by crew: /crew land deletes this file. Delete it by hand only if crew is gone.
name: Explore
description: Fast read-only search helper. Use it to find files, search code for keywords, and answer questions about where things are in a codebase. Say how thorough to be. Reports findings with file paths; it does not judge or edit code.
model: haiku
disallowedTools: Write, Edit, NotebookEdit, Agent
---
You are a read-only search helper running on a small, fast model.

- Find what you were asked for and report it briefly: file paths with line numbers, and the few lines that matter.
- Never create, edit, move or delete files. Use only commands that read.
- Never call the advisor tool, even if the task asks you to. It costs far more than you do; only the main chat may use it.
- If you find nothing, say exactly where and how you searched, so the main chat can check. Never say something does not exist; say you did not find it.
"""


class Stop(Exception):
    """A reason to stop without changing anything."""


# ---------- small helpers ----------

def key_name(path):
    return ".".join(path)


def get_path(d, path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return NOT_SET
        d = d[p]
    return d


def set_path(d, path, value):
    for p in path[:-1]:
        if p not in d:
            d[p] = {}
        elif not isinstance(d[p], dict):
            raise Stop(f"'{p}' in the settings file is not an object, so crew won't touch it. Nothing changed.")
        d = d[p]
    d[path[-1]] = value


def missing_parents(d, path):
    """The parent objects along path that don't exist yet (crew will create them, and only those may be pruned)."""
    out = []
    for i in range(1, len(path)):
        parent = get_path(d, path[:i])
        if parent == NOT_SET:
            out.append(list(path[:i]))
        elif not isinstance(parent, dict):
            raise Stop(f"'{'.'.join(path[:i])}' in the settings file is not an object, so crew won't touch it. "
                       "Nothing changed.")
    return out


def unset_path(d, path, prune=()):
    """Remove a value. Then remove parent objects that crew itself created (listed in prune) if now empty."""
    chain = [d]
    for p in path[:-1]:
        if not isinstance(chain[-1], dict) or p not in chain[-1]:
            return
        chain.append(chain[-1][p])
    if isinstance(chain[-1], dict):
        chain[-1].pop(path[-1], None)
    prune = {tuple(x) for x in prune}
    for i in range(len(path) - 1, 0, -1):
        if chain[i] == {} and tuple(path[:i]) in prune:
            chain[i - 1].pop(path[i - 1], None)
        else:
            break


def flat(d, prefix=""):
    out = {}
    if isinstance(d, dict) and d:
        for k, v in d.items():
            out.update(flat(v, prefix + k + "."))
    else:
        out[prefix[:-1]] = d
    return out


def show(v):
    return "not set" if v == NOT_SET else json.dumps(v)


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def read_settings():
    """Read the settings file fresh. Returns (raw text, parsed dict)."""
    if not os.path.exists(SETTINGS):
        return "", {}
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        raise Stop(f"Can't read {SETTINGS}: {e}")
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        raise Stop(f"{SETTINGS} is not valid JSON ({e}). Nothing changed.")
    if not isinstance(data, dict):
        raise Stop(f"{SETTINGS} is not a JSON object. Nothing changed.")
    return raw, data


def indent_of(raw):
    for line in raw.splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped and len(stripped) < len(line):
            ws = line[: len(line) - len(stripped)]
            return "\t" if ws.startswith("\t") else len(ws)
    return 2


def write_settings(raw_before, data_before, changes, expect=None, prune=()):
    """Apply changes [(path, value or NOT_SET)] to a fresh read, save in one step, verify.

    Re-reads the file just before writing so a change another tool made a moment ago is kept. `expect` lists
    (path, value) pairs the fresh read must still hold; if any moved, nothing is written (the caller's
    decisions were based on the older read).
    """
    raw, data = read_settings()
    if raw != raw_before:
        data_before = data
    for path, value in (expect or []):
        if get_path(data_before, path) != value:
            raise Stop(f"{key_name(path)} changed while crew was working. Nothing changed; run the command again.")
    new = json.loads(json.dumps(data_before))
    for path, value in changes:
        if value == NOT_SET:
            unset_path(new, path, prune)
        else:
            set_path(new, path, value)
    text = json.dumps(new, indent=indent_of(raw), ensure_ascii=False)
    if raw.endswith("\n") or not raw:
        text += "\n"
    folder = os.path.dirname(SETTINGS)
    os.makedirs(folder, exist_ok=True)
    mode = os.stat(SETTINGS).st_mode & 0o777 if os.path.exists(SETTINGS) else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".settings.crew.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, SETTINGS)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise Stop(f"Can't write {SETTINGS}: {e}. Nothing changed.")
    # Verify: valid, the named values are as asked, every other value unchanged.
    _, after = read_settings()
    named = {key_name(p) for p, _ in changes}
    fb, fa = flat(data_before), flat(after)
    stray = [k for k in set(fb) | set(fa)
             if fb.get(k, NOT_SET) != fa.get(k, NOT_SET)
             and not any(k == n or k.startswith(n + ".") or n.startswith(k + ".") for n in named)]
    wrong = [key_name(p) for p, v in changes if get_path(after, p) != v]
    if stray or wrong:
        raise Stop("Settings check failed after saving. Unexpected: "
                   + ", ".join(stray + wrong) + ". See 'Undo by hand' in the crew README.")
    return after


def receipt_path(data_dir):
    return os.path.join(data_dir, "receipt.json")


def load_receipt(data_dir):
    p = receipt_path(data_dir)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_receipt(data_dir, receipt):
    os.makedirs(data_dir, exist_ok=True)
    p = receipt_path(data_dir)
    fd, tmp = tempfile.mkstemp(prefix=".receipt.", dir=data_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2)
    os.replace(tmp, p)


def owned_backup(path):
    """Only marked receipts belong to crew; leave foreign or unreadable files alone."""
    try:
        with open(path, encoding="utf-8") as f:
            receipt = json.load(f)
        return receipt if isinstance(receipt, dict) and receipt.get("made_by") == "crew" else None
    except (OSError, ValueError):
        return None


def backup_paths(receipt):
    paths = [BACKUP]
    if receipt.get("keep_dir"):
        paths.append(os.path.join(receipt["keep_dir"], "crew-receipt.json"))
    return list(dict.fromkeys(paths))


def check_backups_free(receipt):
    for path in backup_paths(receipt):
        if os.path.lexists(path):
            raise Stop(f"Receipt backup already exists at {path}. Land or move it first; crew won't overwrite it.")


def save_backups(receipt):
    check_backups_free(receipt)
    for path in backup_paths(receipt):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "x", encoding="utf-8") as f:
                json.dump(receipt, f, indent=2)
                f.write("\n")
        except OSError as e:
            raise Stop(f"Can't save receipt backup {path}: {e}. Run /crew land before trying again.")


def remove_backups(receipt):
    for path in backup_paths(receipt):
        if owned_backup(path):
            os.remove(path)


def explore_state():
    """'none', 'crew' (has the marker in its first lines) or 'other'."""
    if not os.path.exists(EXPLORE):
        return "none"
    try:
        with open(EXPLORE, encoding="utf-8") as f:
            head = f.read(400).lower()
    except OSError:
        return "other"
    return "crew" if MARKER in head else "other"


def sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def listing(sub):
    root = os.path.join(HOME, sub)
    return sorted(os.listdir(root)) if os.path.isdir(root) else []


def snapshot():
    """The narrow check: what crew could disturb. Values are kept as hashes only."""
    _, s = read_settings()
    return {
        "taken": now(),
        "settings": {k: hashlib.sha256(json.dumps(v).encode()).hexdigest()[:16]
                     for k, v in flat(s).items()},
        "settings_sha": sha(SETTINGS),
        "claude_md_sha": sha(os.path.join(HOME, "CLAUDE.md")),
        "agents": listing("agents"),
        "commands": listing("commands"),
        "skills": listing("skills"),
        "hooks": listing("hooks"),
    }


def compare(a, b):
    diffs = []
    for k in sorted(set(a["settings"]) | set(b["settings"])):
        if a["settings"].get(k) != b["settings"].get(k):
            diffs.append(f"setting {k} differs")
    if a["claude_md_sha"] != b["claude_md_sha"]:
        diffs.append("CLAUDE.md differs")
    for key in ("agents", "commands", "skills", "hooks"):
        added = sorted(set(b[key]) - set(a[key]))
        gone = sorted(set(a[key]) - set(b[key]))
        if added:
            diffs.append(f"{key} added: {', '.join(added)}")
        if gone:
            diffs.append(f"{key} removed: {', '.join(gone)}")
    return diffs


# ---------- history, keep folder, advisor check ----------

def config(data_dir):
    try:
        with open(os.path.join(data_dir, "config.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def record(data_dir, event, **extra):
    """Append takeoff/land to history.jsonl in the data folder, and in the keep folder if set."""
    line = json.dumps(dict({"event": event, "when": now()}, **extra))
    folders = [data_dir] + ([config(data_dir)["keep_dir"]] if config(data_dir).get("keep_dir") else [])
    for folder in folders:
        try:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "history.jsonl"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def advisor_active():
    """Ask Claude Code which advisor a new chat gets. Uses no tokens. None if it can't tell."""
    if os.environ.get("CREW_NO_VERIFY"):
        return None
    import subprocess
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PLUGIN_DATA",
                        "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")}
    try:
        r = subprocess.run(["claude", "-p", "/advisor", "--no-session-persistence"], capture_output=True, text=True,
                           timeout=60, env=env, cwd=os.path.expanduser("~"))
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    for line in lines:
        if line.startswith("Advisor:"):
            return line.split(":", 1)[1].strip()
    # e.g. "/advisor isn't available in this environment" when the advisor is switched off for this Mac
    return lines[0] if lines else None


def landing_card(since_iso):
    if os.environ.get("CREW_NO_CARD"):
        return "(landing card skipped)"
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import crew_flight
        return crew_flight.landing(datetime.datetime.fromisoformat(since_iso).astimezone())
    except Exception as e:  # the card is a bonus; landing must not fail because of it
        return f"(Landing card unavailable: {e.__class__.__name__})"


# ---------- warnings about things that win over crew ----------

def overrides(cwd):
    notes = []
    for var in ("ANTHROPIC_MODEL", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL"):
        if os.environ.get(var):
            notes.append(f"{var} is set in your shell; it wins over crew's settings.")
    if cwd:
        for name in ("settings.json", "settings.local.json"):
            p = os.path.join(cwd, ".claude", name)
            try:
                with open(p, encoding="utf-8") as f:
                    s = json.load(f)
            except (OSError, ValueError):
                continue
            hits = [k for k in ("model", "effortLevel", "modelSettings", "advisorModel") if k in s]
            if hits:
                notes.append(f"This project's .claude/{name} sets {', '.join(hits)}; it wins over crew here.")
    return notes


def managed_settings():
    paths = ["/Library/Application Support/ClaudeCode/managed-settings.json",
             "/etc/claude-code/managed-settings.json",
             r"C:\Program Files\ClaudeCode\managed-settings.json"]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    s = json.load(f)
            except (OSError, ValueError):
                return p
            if any(k in s for k in ("model", "advisorModel", "modelSettings", "effortLevel", "availableModels")):
                return p
    return None


# ---------- one crew command at a time ----------

class Lock:
    """Holds an exclusive lock in the data folder for the whole takeoff or land."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, ".lock")
        self.f = None
        os.makedirs(data_dir, exist_ok=True)

    def __enter__(self):
        self.f = open(self.path, "w")
        try:
            import fcntl
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.f.close()
                raise Stop("Another crew command is running. Nothing changed; try again in a moment.")
        except ImportError:  # Windows: no fcntl; carry on without the lock
            pass
        return self

    def __exit__(self, *exc):
        try:
            self.f.close()
        except Exception:
            pass
        return False


# ---------- commands ----------

def takeoff(data_dir, cwd=None):
    out = []
    existing = load_receipt(data_dir) or owned_backup(BACKUP)
    if existing:
        out.append("Crew is already on (since " + existing.get("taken_at", "?") + "). Nothing changed.")
        out.append("Type /crew land to turn it off.")
        return out
    m = managed_settings()
    if m:
        raise Stop(f"Your organization's settings ({m}) control the model. Crew changed nothing.")
    raw, data = read_settings()
    if not os.access(SETTINGS if os.path.exists(SETTINGS) else os.path.dirname(SETTINGS), os.W_OK):
        raise Stop(f"Can't write {SETTINGS}. Crew changed nothing.")
    ex = explore_state()
    if ex == "other":
        raise Stop("You already have your own ~/.claude/agents/Explore.md (not made by crew). "
                   "Crew won't overwrite it and changed nothing. Rename or move it, then run /crew takeoff again.")
    values, created_parents = [], []
    for path, new in TARGETS:
        values.append({"key": key_name(path), "old": get_path(data, path), "new": new})
        for parent in missing_parents(data, path):
            if parent not in created_parents:
                created_parents.append(parent)
    receipt = {
        "made_by": "crew",
        "keep_dir": config(data_dir).get("keep_dir"),
        "taken_at": now(),
        "settings_file": SETTINGS,
        "values": values,
        "files_created": [EXPLORE] if ex == "none" else [],
        "explore_was_leftover": ex == "crew",
        "created_parents": created_parents,
        "snapshot_before": snapshot(),
    }
    receipt["undo_by_hand"] = [
        f'remove "{v["key"]}"' if v["old"] == NOT_SET
        else f'set "{v["key"]}" back to {json.dumps(v["old"])}'
        for v in values
    ]
    receipt["undo_by_hand"].extend(
        f"delete {p}" for p in receipt["files_created"] + ([EXPLORE] if ex == "crew" else []))
    check_backups_free(receipt)  # before anything is written, so a foreign file there changes nothing
    # Receipt first, so a takeoff that stops halfway can still be undone.
    save_receipt(data_dir, receipt)
    save_backups(receipt)
    if ex == "none":
        os.makedirs(AGENTS, exist_ok=True)
        new_folder = not listing("agents")
        try:
            with open(EXPLORE, "x", encoding="utf-8") as f:  # never overwrite a file that appeared meanwhile
                f.write(EXPLORE_TEXT)
        except FileExistsError:
            remove_backups(receipt)
            os.remove(receipt_path(data_dir))
            raise Stop("An Explore.md appeared in ~/.claude/agents/ while crew was starting. Crew changed nothing.")
        if new_folder:
            out.append("Note: ~/.claude/agents/ was empty, so Claude Code needs a restart to see the new search helper.")
    changes = [(tuple(v["key"].split(".")), v["new"]) for v in values if v["old"] != v["new"]]
    if changes:
        write_settings(raw, data, changes,
                       expect=[(tuple(v["key"].split(".")), v["old"]) for v in values])
    record(data_dir, "takeoff")
    out.append("Crew is on.")
    for v in values:
        label = LABELS[v["key"]]
        if v["old"] == v["new"]:
            out.append(f"  {label}: {show(v['new'])} (already)")
        else:
            out.append(f"  {label}: {show(v['old'])} -> {show(v['new'])}")
    out.append("  search helper: Explore on Haiku (~/.claude/agents/Explore.md)")
    olds = ", ".join(f"{LABELS[v['key']]} {show(v['old'])}" for v in values)
    out.append(f"To undo by hand if crew is ever gone: set {olds} (see 'Undo by hand' in the crew README).")
    out.append(f"Receipt and manual undo steps saved outside the plugin: {BACKUP}")
    for n in overrides(cwd or os.getcwd()):
        out.append("Warning: " + n)
    adv = advisor_active()
    if adv is not None and "fable" not in adv.lower():
        out.append(f"Note: the advisor is not active (Claude Code says: {adv}). Your plan may need Fable access or a "
                   "one-time consent that only you can give with /model fable. Crew gives no consent itself.")
    out.append(NEW_CHAT)
    return out


def land(data_dir):
    out = []
    receipt = load_receipt(data_dir) or owned_backup(BACKUP)
    if not receipt:
        out.append("Crew is off. Nothing to do.")
        return out
    raw, data = read_settings()
    changes, kept = [], []
    receipt_new = {v["key"]: v["new"] for v in receipt["values"]}
    for v in receipt["values"]:
        path = tuple(v["key"].split("."))
        now_v = get_path(data, path)
        if v["old"] == v["new"]:
            continue  # takeoff didn't change it
        if now_v == v["new"]:
            changes.append((path, v["old"]))
        else:
            kept.append((v, now_v))
    if changes:
        write_settings(raw, data, changes,
                       expect=[(path, receipt_new[key_name(path)]) for path, _ in changes],
                       prune=receipt.get("created_parents", []))
    removed = []
    for p in receipt.get("files_created", []) + ([EXPLORE] if receipt.get("explore_was_leftover") else []):
        if p == EXPLORE and explore_state() != "crew":
            if os.path.exists(p):
                out.append(f"Left {p} alone: it no longer has crew's marker.")
            continue
        try:
            os.remove(p)
            removed.append(p)
        except FileNotFoundError:
            pass
    after = snapshot()
    diffs = compare(receipt["snapshot_before"], after)
    remove_backups(receipt)
    if os.path.exists(receipt_path(data_dir)):
        os.remove(receipt_path(data_dir))
    record(data_dir, "land")
    card = landing_card(receipt["taken_at"])
    out.append("Crew is off.")
    for path, old in changes:
        out.append(f"  {LABELS[key_name(path)]}: back to {show(old)}")
    for v, now_v in kept:
        if now_v == v["old"]:
            out.append(f"  {LABELS[v['key']]}: already back to {show(now_v)} (changed by hand after takeoff).")
        else:
            out.append(f"  {LABELS[v['key']]}: left at {show(now_v)} because it was changed after takeoff "
                       f"(before crew it was {show(v['old'])}).")
    for p in removed:
        out.append(f"  removed {p.replace(os.path.expanduser('~'), '~')}")
    if diffs:
        out.append("Changed since takeoff by you or another tool (crew left these alone):")
        out.extend("  " + d for d in diffs)
    else:
        out.append("Clean check: settings, helpers, commands, skills, hooks and CLAUDE.md match the moment before takeoff.")
    out.append(NEW_CHAT)
    out.append("")
    out.append(card)
    keep = config(data_dir).get("keep_dir")
    if keep:
        try:
            os.makedirs(os.path.join(keep, "landings"), exist_ok=True)
            p = os.path.join(keep, "landings", "landing-" + now().replace(":", "")[:15] + ".md")
            with open(p, "w", encoding="utf-8") as f:
                f.write(card + "\n")
            out.append(f"Card saved: {p}")
        except OSError:
            pass
    return out


def status(data_dir):
    out = []
    receipt = load_receipt(data_dir)
    _, data = read_settings()
    out.append("Crew is " + ("ON since " + receipt["taken_at"] if receipt else "off") + ".")
    if receipt:
        for v in receipt["values"]:
            cur = get_path(data, tuple(v["key"].split(".")))
            flag = "" if cur == v["new"] else "  (changed after takeoff)"
            out.append(f"  {LABELS[v['key']]}: now {show(cur)}, crew set {show(v['new'])}, before crew {show(v['old'])}{flag}")
        out.append("  search helper: " + {"crew": "Explore on Haiku", "none": "built-in (crew's file is gone)",
                                           "other": "your own Explore.md"}[explore_state()])
    else:
        for path, new in TARGETS:
            out.append(f"  {LABELS[key_name(path)]}: now {show(get_path(data, path))} (crew would set {show(new)})")
    out.append("Commands: /crew takeoff (or /crew on), /crew land (or /crew off), /crew boost.")
    return out


def log(data_dir):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import crew_flight
    keep = config(data_dir).get("keep_dir")
    out = os.path.join(keep or data_dir, "flight-log.html")
    end = datetime.date.today()
    hist = []
    try:
        hist = [json.loads(x) for x in open(os.path.join(keep or data_dir, "history.jsonl"), encoding="utf-8")]
    except (OSError, ValueError):
        pass
    takeoffs = [h["when"] for h in hist if h.get("event") == "takeoff"]
    start = end - datetime.timedelta(days=27)
    before = None
    if takeoffs:
        first = datetime.date.fromisoformat(takeoffs[0][:10])
        start = min(start, first - datetime.timedelta(days=7))
        before = (first - datetime.timedelta(days=28), first - datetime.timedelta(days=1))
    crew_flight.page(start, end, out, before=before, data_dir=keep or data_dir)
    return [f"Flight log saved: {out}"]


def keep(data_dir, folder):
    folder = os.path.abspath(os.path.expanduser(folder))
    os.makedirs(folder, exist_ok=True)
    c = config(data_dir)
    c["keep_dir"] = folder
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)
    return [f"Landing cards and the flight log will also be saved in {folder} (kept if crew is uninstalled)."]


def check():
    """Leftover check, what the README's "Undo by hand" section checks."""
    _, s = read_settings()
    out = [
        "model: " + str(s.get("model", "not set")),
        "advisor: " + str(s.get("advisorModel", "none")),
        "opus effort: " + str(s.get("modelSettings", {}).get(OPUS_ID, {}).get("effortLevel", "not saved")),
    ]
    lines = [k for k in list(s.get("enabledPlugins", {})) + list(s.get("extraKnownMarketplaces", {}))
             if "crew" in k.lower()]
    out.append("crew lines in settings: " + (str(lines) if lines else "none"))
    import glob
    files = [p for p in glob.glob(os.path.join(AGENTS, "*.md")) if MARKER in open(p, encoding="utf-8").read(400).lower()]
    if owned_backup(BACKUP):
        files.append(BACKUP)
    files += glob.glob(os.path.join(HOME, "plugins", "data", "crew*"))
    files += glob.glob(os.path.join(HOME, "plugins", "cache", "*crew*"))
    out.append("crew files: " + (str(files) if files else "none"))
    return out


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    data_dir = None
    if "--data" in argv:
        i = argv.index("--data")
        data_dir = argv[i + 1] if i + 1 < len(argv) else None
    cwd = None
    if "--cwd" in argv:
        i = argv.index("--cwd")
        cwd = argv[i + 1] if i + 1 < len(argv) else None
    aliases = {"on": "takeoff", "off": "land"}
    cmd = aliases.get(cmd, cmd)
    try:
        if cmd == "check":
            lines = check()
        elif cmd in ("takeoff", "land", "status", "log", "keep"):
            if not data_dir or "${" in data_dir:
                raise Stop("Crew's data folder is unknown. Run this through /crew, not by hand.")
            if cmd == "takeoff":
                with Lock(data_dir):
                    lines = takeoff(data_dir, cwd)
            elif cmd == "land":
                with Lock(data_dir):
                    lines = land(data_dir)
            elif cmd == "log":
                lines = log(data_dir)
            elif cmd == "keep":
                rest = [a for a in argv[1:] if a not in ("--data", data_dir)]
                if not rest:
                    raise Stop("keep needs a folder.")
                lines = keep(data_dir, rest[0])
            else:
                lines = status(data_dir)
        else:
            print(f"Unknown command {cmd!r}. Use takeoff, land, status, log, keep or check.")
            return 2
    except Stop as e:
        print("Crew stopped: " + str(e))
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
