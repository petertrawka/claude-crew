#!/usr/bin/env python3
"""Start-of-chat hook: add crew's rule block, but only while crew is on.

Crew is on when its receipt exists in the plugin's data folder. Otherwise this prints
nothing. Any error also prints nothing and exits 0, so a chat always opens normally.
"""
import json
import os
import sys

RULES = """Crew rules:
1. An empty helper search is not proof; check before saying something doesn't exist.
2. If a test, build or script can check the job, run it before saying done.
3. Call the advisor only when the same fix failed twice, the user corrected you twice, or the user typed /crew boost; in a main chat with the user present, also once before a big multi-file job. Never at the start of a helper or ticket build, never as a routine pre-done check, never on Fable. Say in one line why you called it.
4. Start every helper prompt with "Do not call the advisor tool." No code writing by Haiku helpers.
5. Suggest a handoff or /compact before a chat gets long (each advisor call rereads it all).
6. For finding files or searching code, use the Explore helper (runs on Haiku); for reading long logs or test output, use crew:log-reader. Never use general-purpose for these: it runs on the main model. general-purpose is only for jobs that need judgment or edits. Explore finds where things are; it does not judge how code works. One narrow question per call. Open the file yourself before acting on what it reports."""


def main():
    try:
        sys.stdin.read()
    except Exception:
        pass
    try:
        data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
        if not data or not os.path.exists(os.path.join(data, "receipt.json")):
            return
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": RULES}}))
    except Exception:
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
