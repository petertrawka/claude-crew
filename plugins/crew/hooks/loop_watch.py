#!/usr/bin/env python3
"""Nudge once per test/build/check command per session after it fails twice in a row with the same error.

https://code.claude.com/docs/en/hooks#posttoolusefailure documents failed Bash
calls with hook_event_name=PostToolUseFailure and error='Exit code N\n...'.
That event is authoritative (interrupts are ignored). Otherwise prefer explicit
exit_code/exitCode or is_error fields, then an Exit code N output line, then
failure markers in stdout/stderr/output. With no failure signal, PostToolUse
counts as a pass: the docs say it fires after successful execution. Output-only
heuristics can misclassify text; explicit status always wins.

Only active while CLAUDE_PLUGIN_DATA/receipt.json exists. Any error: print
nothing, exit 0. Never block. Keep at most 50 commands; retain nudged keys so
an evicted command cannot trigger a second nudge in the same session.
"""
import json
import os
import re
import shlex
import sys


def command_key(command):
    if not isinstance(command, str) or len(command) > 2048:
        return None
    words = shlex.split(command)
    while words and words[0] == 'cd' and '&&' in words:
        words = words[words.index('&&') + 1:]
    while words and words[-1] == '2>&1':
        words.pop()
    if '|' in words:
        index = words.index('|')
        if words[index + 1:index + 2] == ['tail']:
            words = words[:index]
    while words and words[-1] == '2>&1':
        words.pop()
    if not words:
        return None
    probe = words[:]
    while probe and re.match(r'^[A-Za-z_][A-Za-z_0-9]*=', probe[0]):
        probe.pop(0)
    if probe[:2] == ['uv', 'run']:
        probe = probe[2:]
    if probe[:1] == ['npx']:
        probe = probe[1:]
    if not probe:
        return None
    executable = os.path.basename(probe[0])
    args = probe[1:]
    direct = {'pytest', 'jest', 'vitest', 'tsc', 'ruff', 'mypy', 'eslint'}
    matched = executable in direct
    if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', executable):
        matched = len(args) >= 2 and args[0] == '-m' and args[1] in {'pytest', 'unittest', 'ruff', 'mypy'}
    elif executable in {'npm', 'pnpm', 'yarn'}:
        if args[:1] == ['run']:
            args = args[1:]
        matched = bool(args) and args[0] in {'test', 'build', 'check', 'lint', 'typecheck'}
    elif executable in {'go', 'cargo', 'make', 'swift'}:
        matched = bool(args) and args[0] in {'test', 'build', 'check'}
    elif executable == 'node':
        matched = '--test' in args
    return shlex.join(words) if matched else None


def failed(event):
    if event['hook_event_name'] == 'PostToolUseFailure':
        return True
    response = event.get('tool_response', {})
    for obj in (response, event):
        if isinstance(obj, dict):
            for key in ('exit_code', 'exitCode'):
                if key in obj:
                    return int(obj[key]) != 0
            if isinstance(obj.get('is_error'), bool):
                return obj['is_error']
    if isinstance(response, dict):
        output = '\n'.join(str(response.get(k, '')) for k in ('stdout', 'stderr', 'output', 'error'))
    else:
        output = str(response)
    code = re.search(r'\bexit(?: code| status|ed with (?:code|status))\s*:?\s*(-?\d+)\b', output, re.I)
    if code:
        return int(code[1]) != 0
    output = re.sub(r'\b0 (?:failed|failures|errors)\b', '', output, flags=re.I)
    return bool(re.search(r'\b(?:failed|failure[s]?)\b|\berror:|\berror TS\d+|\b[1-9]\d* errors?\b', output, re.I))


ERROR_LINE = re.compile(r'FAILED|ERROR|Error|error[:\s]|assert|Exception|Traceback|panicked|\bfailed\b|✗|×')


def error_signature(event):
    """A fingerprint of the error, so a changed error counts as progress, not a loop.

    Takes the lines that look like errors (or the last 5 lines if none do), drops timings,
    memory addresses and temp-folder names that change on every run, and hashes the rest.
    """
    response = event.get('tool_response', {})
    parts = [str(event.get('error', ''))]
    if isinstance(response, dict):
        parts += [str(response.get(k, '')) for k in ('stdout', 'stderr', 'output', 'error')]
    else:
        parts.append(str(response))
    lines = [l.strip() for l in '\n'.join(parts).splitlines() if l.strip()]
    picked = [l for l in lines if ERROR_LINE.search(l)] or lines[-5:]
    clean = []
    for l in picked:
        l = re.sub(r'\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b', '<t>', l)
        l = re.sub(r'0x[0-9a-fA-F]+', '<addr>', l)
        l = re.sub(r'/(?:private/)?(?:tmp|var/folders)/\S+', '<tmp>', l)
        clean.append(l)
    import hashlib
    return hashlib.sha1('\n'.join(sorted(set(clean)))[:20000].encode()).hexdigest()[:16]

def main():
    try:
        data = os.environ.get('CLAUDE_PLUGIN_DATA', '')
        if not data or not os.path.exists(os.path.join(data, 'receipt.json')):
            return
        event = json.load(sys.stdin)
        name = event.get('hook_event_name')
        if event.get('tool_name') != 'Bash' or name not in ('PostToolUse', 'PostToolUseFailure'):
            return
        if event.get('is_interrupt') or (isinstance(event.get('tool_response'), dict)
                                         and event['tool_response'].get('interrupted')):
            return
        session = event.get('session_id', '')
        if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session):
            return
        key = command_key(event.get('tool_input', {}).get('command'))
        if not key:
            return
        failure = failed(event)
        folder = os.path.join(data, 'loops')
        os.makedirs(folder, exist_ok=True)
        # Lock the state itself so parallel Bash completions cannot lose updates.
        import fcntl
        fd = os.open(os.path.join(folder, session + '.json'), os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'r+', encoding='utf-8') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            raw = f.read()
            state = json.loads(raw) if raw else {}
            if key not in state and len(state) >= 50:
                victim = next((k for k, v in state.items() if not v['nudged']), None)
                if victim is None:
                    return
                del state[victim]
            entry = state.setdefault(key, {'failures': 0, 'nudged': False})
            if failure:
                sig = error_signature(event)
                # Twice only if the error is the same; a new error means the last fix changed something.
                entry['failures'] = 2 if entry['failures'] >= 1 and entry.get('sig') == sig else 1
                entry['sig'] = sig
            else:
                entry['failures'] = 0
                entry.pop('sig', None)
            nudge = entry['failures'] == 2 and not entry['nudged']
            if nudge:
                entry['nudged'] = True
            f.seek(0)
            json.dump(state, f)
            f.truncate()
        if nudge:
            short = key if len(key) <= 120 else key[:117] + '...'
            print(json.dumps({'hookSpecificOutput': {
                'hookEventName': name,
                'additionalContext': f'Crew: `{short}` has failed twice in a row. Before a third try: '
                "stop, say in one line what you've tried, and change approach or call the advisor "
                '(rule 3: same fix failed twice).'}}))
    except Exception:
        return


if __name__ == '__main__':
    main()
    sys.exit(0)
