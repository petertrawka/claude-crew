---
name: crew
description: Crew switch and tools. /crew takeoff (or on) turns crew on, /crew land (or off) puts everything back, /crew boost asks the advisor when a job is stuck, /crew log opens the flight log, /crew pace shows this week's pace, /crew alone shows the state.
argument-hint: "[takeoff | land | on | off | boost | log | pace]"
disable-model-invocation: true
---

# /crew

The user typed `/crew $ARGUMENTS`. Take the first word of the arguments (lower case) and do exactly one of
the sections below. Crew's script is `${CLAUDE_PLUGIN_ROOT}/scripts/crew.py` and its data folder is
`${CLAUDE_PLUGIN_DATA}`. Never edit `~/.claude/settings.json` yourself; only the script does that.

## No word, `status` or anything unknown

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/crew.py" status --data "${CLAUDE_PLUGIN_DATA}"` and show its output
as it is, in a plain code block. Nothing else.

## `takeoff` or `on`

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/crew.py" takeoff --data "${CLAUDE_PLUGIN_DATA}" --cwd "$PWD"`.
2. Show its output as it is, in a plain code block. Add nothing except what the next step says.
3. If the output starts with "Crew stopped" because of an existing `Explore.md`, tell the user in one line to
   rename or move their file and run /crew takeoff again. Do not move it yourself.
4. Never accept or give any Fable consent, and never run `/model` or `/advisor` yourself.

## `land` or `off`

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/crew.py" land --data "${CLAUDE_PLUGIN_DATA}"`.
2. Show its output as it is, in a plain code block (it ends with the landing card). Add nothing.

## `log`

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/crew.py" log --data "${CLAUDE_PLUGIN_DATA}"`. It builds the flight
log page and prints where it saved it. Open that file with `open "<path>"` (macOS) and say in one line where it is.

## `keep`

The rest of the arguments is a folder. Run
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/crew.py" keep --data "${CLAUDE_PLUGIN_DATA}" "<folder>"` and show the one
line it prints. From then on, landing cards and the flight log are also saved in that folder.

## `pace`

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/crew_flight.py" pace`. Reply with only the line it printed, in a plain
code block. Nothing before or after it.

## `boost`

The user feels this job is stuck. First run `test -f "${CLAUDE_PLUGIN_DATA}/receipt.json" && echo on || echo off`.

- **off:** reply only "Crew is off, so /crew boost does nothing. Type /crew takeoff to turn it on." Ask nothing,
  call nothing.

- **on**, and this chat runs on **Opus or Sonnet**, and this is the **first** `/crew boost` in this chat, and the
  last attempt after an earlier boost has not already failed:
  1. In your head (don't write it out), sum up what has been tried and what keeps failing.
  2. Call the advisor tool now. If you have no advisor tool, or it says it is unavailable or declined, say so in one
     line ("The advisor is off or unavailable, so going straight to the next step.") and go to the handoff step below.
  3. Reply in exactly this shape, and keep it that short:
     ```
     Asked the advisor because you typed /crew boost.
     Advice: <two or three short plain lines, no lists>
     Direction: <changes or stays, in one line>
     ```
  4. Then try once more, following the advice, and run the test or check before saying it worked. If nothing can be
     tried from here (no files, no access), say in one line what you need instead.

- **on**, and this is the **second** `/crew boost` in this chat, or the try after the advice failed, or the advisor
  was unavailable: suggest a fresh chat on Fable medium. Do not switch anything yourself. Write:
  - The corrected direction, in two or three lines, ready to paste into the handoff (what was wrong with the old
    approach, what to do instead, how to check it).
  - "To do it: save a handoff note (your /handoff command if you have one, or ask me to write the state to a file), then in the new chat type /model, choose Fable 5.2, move the effort to medium, and
    press s (this session only, so your default stays the same)."
  - The price, in one line: "Fable medium costs about the same per job as Opus high and 1.5 to 2 times Opus
    medium; on most plans it comes out of the weekly limit (on some plans Fable can use at most half of it)."

- **on**, and this chat runs on **Fable** (you are Fable): do not call the advisor. Check the effort with
  `echo "$CLAUDE_EFFORT"`.
  - medium: say the next step up is Fable high, which is the user's choice only: about 3 to 4 times the cost of
    Fable medium per job. Give the steps: save a handoff note, then /model, Fable 5.2, effort high, press s. Also give the corrected
    direction lines as above.
  - high (or anything higher): say this is the top step. Suggest splitting the job into smaller parts or rethinking
    the approach, and list the two or three parts you would split it into.

Never suggest low effort for building, and never suggest xhigh or max. Nothing switches by itself: you only suggest.
