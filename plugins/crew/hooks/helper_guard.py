#!/usr/bin/env python3
"""Before-helper hook (PreToolUse on Agent): enforce crew rules 4 and 6 in code, only while crew is on.

- Every helper prompt gets "Do not call the advisor tool." in front, if it doesn't start with it already.
- A general-purpose helper (or one with no type, which is general-purpose) asked only to search or read
  logs is refused, with the reason telling Claude to use Explore or crew:log-reader instead.

Crew is on when its receipt exists in the plugin's data folder. Off, or any error: print nothing, exit 0,
so the helper starts exactly as it would without crew.
"""
import json
import os
import re
import sys

LINE = "Do not call the advisor tool."

# The job starts with a search verb ...
SEARCH = re.compile(r"^\s*(find|search|locate|grep|look for|look up where|where (is|are|do|does)|"
                    r"list (all |the )?(files|places|uses|callers)|which files?|read (the |this )?(log|output))\b",
                    re.I)
# ... and nowhere asks for judgment or edits.
WORK = re.compile(r"\b(edit|fix|write|implement|change|refactor|build|create|update|modify|delete|"
                  r"review|decide|design|plan|explain why|judge|test|run)\b", re.I)


def is_search_only(text):
    return bool(SEARCH.search(text)) and not WORK.search(text)


def decide(tool_input):
    """Return the hook output dict, or None to say nothing."""
    if not isinstance(tool_input, dict):
        return None
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str):
        return None
    kind = (tool_input.get("subagent_type") or "general-purpose").strip()
    body = prompt.strip()
    if body.lower().startswith(LINE.lower()):
        body = body[len(LINE):].strip()

    desc = tool_input.get("description") or ""
    searchy = is_search_only(body) or (is_search_only(desc) and not WORK.search(body))
    if kind == "general-purpose" and searchy:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Crew rule 6: this looks like a search or log read, which costs main-model prices on "
                "general-purpose. Use subagent_type \"Explore\" for finding files or code, or "
                "\"crew:log-reader\" for long logs. If the job really needs judgment or edits, say so in the "
                "prompt (for example \"then fix ...\") and launch it again.")}}

    if prompt.lstrip().lower().startswith(LINE.lower()):
        return None
    new_input = dict(tool_input)
    new_input["prompt"] = LINE + "\n\n" + prompt
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "Crew rule 4: added the no-advisor line to the helper prompt.",
        "updatedInput": new_input}}


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    try:
        data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
        if not data or not os.path.exists(os.path.join(data, "receipt.json")):
            return
        event = json.loads(raw)
        if event.get("tool_name") != "Agent":
            return
        out = decide(event.get("tool_input"))
        if out:
            print(json.dumps(out))
    except Exception:
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
