"""Tests for crew.py against a fake ~/.claude folder. Run: python3 -m unittest crew/tests/test_crew.py"""
import importlib.util
import json
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "plugins", "crew", "scripts", "crew.py")

TYPICAL_SETTINGS = {
    "env": {"CLAUDE_CODE_PLUGIN_DIRS": "/x/mods/example"},
    "model": "opus",
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
    "statusLine": {"type": "command", "command": "sh status.sh"},
    "enabledPlugins": {"codex@openai-codex": True},
    "effortLevel": "high",
    "modelSettings": {
        "claude-opus-5": {"effortLevel": "high"},
        "claude-opus-5-5": {"effortLevel": "high"},
        "claude-fable-5-2": {"effortLevel": "medium"},
    },
    "theme": "dark-ansi",
}


def load(home):
    os.environ["CREW_HOME"] = home
    os.environ["CREW_NO_VERIFY"] = "1"
    os.environ["CREW_NO_CARD"] = "1"
    spec = importlib.util.spec_from_file_location("crew_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CrewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "claude")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.home, "agents"))
        open(os.path.join(self.home, "agents", "my-agent.md"), "w").write("---\nname: x\n---\n")
        self.write(TYPICAL_SETTINGS)
        self.crew = load(self.home)

    def tearDown(self):
        shutil.rmtree(self.tmp)
        os.environ.pop("CREW_HOME", None)

    def write(self, d, raw=None):
        with open(os.path.join(self.home, "settings.json"), "w") as f:
            f.write(raw if raw is not None else json.dumps(d, indent=2) + "\n")

    def raw(self):
        return open(os.path.join(self.home, "settings.json")).read()

    def settings(self):
        return json.loads(self.raw())

    def explore(self):
        return os.path.join(self.home, "agents", "Explore.md")

    def test_takeoff_sets_three_values_and_land_restores_bytes(self):
        before = self.raw()
        out = "\n".join(self.crew.takeoff(self.data))
        s = self.settings()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["advisorModel"], "fable")
        self.assertEqual(s["modelSettings"]["claude-opus-5-5"]["effortLevel"], "medium")
        self.assertEqual(s["effortLevel"], "high")  # top line untouched
        self.assertEqual(s["modelSettings"]["claude-fable-5-2"]["effortLevel"], "medium")
        self.assertEqual(s["env"], TYPICAL_SETTINGS["env"])
        self.assertTrue(os.path.exists(self.explore()))
        self.assertEqual(open(self.explore()).read().splitlines()[0], "---")
        self.assertIn("made by crew", open(self.explore()).read().splitlines()[1])
        self.assertIn("To undo by hand", out)
        self.assertIn("Start a new chat", out)
        self.assertTrue(os.path.exists(os.path.join(self.data, "receipt.json")))
        out = "\n".join(self.crew.land(self.data))
        self.assertEqual(self.raw(), before)  # byte for byte
        self.assertFalse(os.path.exists(self.explore()))
        self.assertFalse(os.path.exists(os.path.join(self.data, "receipt.json")))
        self.assertIn("Clean check", out)
        self.assertIn("Start a new chat", out)

    def test_receipt_records_old_and_new_including_not_set(self):
        self.crew.takeoff(self.data)
        r = json.load(open(os.path.join(self.data, "receipt.json")))
        vals = {v["key"]: v for v in r["values"]}
        self.assertEqual(vals["advisorModel"]["old"], "<not set>")
        self.assertEqual(vals["advisorModel"]["new"], "fable")
        self.assertEqual(vals["modelSettings.claude-opus-5-5.effortLevel"]["old"], "high")
        self.assertEqual(r["files_created"], [self.explore()])

    def test_receipt_written_before_changes(self):
        # Make the settings write fail: the receipt must already exist.
        orig = self.crew.write_settings

        def boom(*a, **k):
            raise self.crew.Stop("simulated failure")
        self.crew.write_settings = boom
        with self.assertRaises(self.crew.Stop):
            self.crew.takeoff(self.data)
        self.crew.write_settings = orig
        self.assertTrue(os.path.exists(os.path.join(self.data, "receipt.json")))
        self.assertTrue(os.path.exists(self.crew.BACKUP))
        # Land still works and leaves everything as it was.
        before = json.dumps(TYPICAL_SETTINGS, indent=2) + "\n"
        self.crew.land(self.data)
        self.assertEqual(self.raw(), before)

    def test_values_not_set_before_are_removed_cleanly(self):
        d = {"theme": "dark"}
        self.write(d)
        before = self.raw()
        self.crew.takeoff(self.data)
        s = self.settings()
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["modelSettings"], {"claude-opus-5-5": {"effortLevel": "medium"}})
        self.crew.land(self.data)
        self.assertEqual(self.raw(), before)

    def test_takeoff_twice_keeps_first_receipt(self):
        self.crew.takeoff(self.data)
        r1 = open(os.path.join(self.data, "receipt.json")).read()
        out = "\n".join(self.crew.takeoff(self.data))
        self.assertIn("already on", out)
        self.assertEqual(open(os.path.join(self.data, "receipt.json")).read(), r1)

    def test_land_when_off_does_nothing(self):
        before = self.raw()
        out = "\n".join(self.crew.land(self.data))
        self.assertIn("Crew is off", out)
        self.assertEqual(self.raw(), before)

    def test_unrelated_change_between_is_kept(self):
        self.crew.takeoff(self.data)
        s = self.settings()
        s["theme"] = "light"
        s["newThing"] = {"a": 1}
        self.write(s)
        out = "\n".join(self.crew.land(self.data))
        s = self.settings()
        self.assertEqual(s["theme"], "light")
        self.assertEqual(s["newThing"], {"a": 1})
        self.assertNotIn("advisorModel", s)
        self.assertEqual(s["modelSettings"]["claude-opus-5-5"]["effortLevel"], "high")
        self.assertIn("Changed since takeoff", out)

    def test_value_changed_by_hand_is_left_alone(self):
        self.crew.takeoff(self.data)
        s = self.settings()
        s["modelSettings"]["claude-opus-5-5"]["effortLevel"] = "xhigh"  # user typed /effort
        s["advisorModel"] = "opus"  # user typed /advisor opus
        self.write(s)
        out = "\n".join(self.crew.land(self.data))
        s = self.settings()
        self.assertEqual(s["modelSettings"]["claude-opus-5-5"]["effortLevel"], "xhigh")
        self.assertEqual(s["advisorModel"], "opus")
        self.assertIn("left at \"xhigh\"", out)
        self.assertIn("before crew it was \"high\"", out)
        self.assertIn("before crew it was not set", out)

    def test_advisor_turned_off_by_hand_is_reported_not_reset(self):
        self.crew.takeoff(self.data)
        s = self.settings()
        del s["advisorModel"]  # /advisor off
        self.write(s)
        out = "\n".join(self.crew.land(self.data))
        self.assertNotIn("advisorModel", self.settings())
        self.assertIn("advisor: already back to not set", out)

    def test_own_explore_stops_takeoff_and_changes_nothing(self):
        open(self.explore(), "w").write("---\nname: Explore\nmodel: sonnet\n---\nmine\n")
        before = self.raw()
        with self.assertRaises(self.crew.Stop):
            self.crew.takeoff(self.data)
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(os.path.join(self.data, "receipt.json")))
        self.assertIn("mine", open(self.explore()).read())

    def test_land_never_deletes_explore_without_marker(self):
        self.crew.takeoff(self.data)
        open(self.explore(), "w").write("---\nname: Explore\n---\nuser replaced it\n")
        out = "\n".join(self.crew.land(self.data))
        self.assertTrue(os.path.exists(self.explore()))
        self.assertIn("no longer has crew's marker", out)

    def test_leftover_crew_explore_is_adopted_and_removed(self):
        open(self.explore(), "w").write(self.crew.EXPLORE_TEXT)
        self.crew.takeoff(self.data)
        self.crew.land(self.data)
        self.assertFalse(os.path.exists(self.explore()))

    def test_invalid_settings_stops_takeoff(self):
        self.write(None, raw="{ not json")
        with self.assertRaises(self.crew.Stop):
            self.crew.takeoff(self.data)
        self.assertEqual(self.raw(), "{ not json")
        self.assertFalse(os.path.exists(os.path.join(self.data, "receipt.json")))
        self.assertFalse(os.path.exists(self.explore()))

    def test_unwritable_settings_stops_takeoff(self):
        p = os.path.join(self.home, "settings.json")
        os.chmod(p, 0o444)
        os.chmod(self.home, 0o555)
        try:
            with self.assertRaises(self.crew.Stop):
                self.crew.takeoff(self.data)
        finally:
            os.chmod(self.home, 0o755)
            os.chmod(p, 0o644)
        self.assertFalse(os.path.exists(os.path.join(self.data, "receipt.json")))

    def test_status_on_and_off(self):
        out = "\n".join(self.crew.status(self.data))
        self.assertIn("Crew is off", out)
        self.crew.takeoff(self.data)
        out = "\n".join(self.crew.status(self.data))
        self.assertIn("Crew is ON", out)
        self.assertIn("advisor: now \"fable\", crew set \"fable\", before crew not set", out)

    def test_check_finds_leftovers(self):
        self.crew.takeoff(self.data)
        out = "\n".join(self.crew.check())
        self.assertIn("advisor: fable", out)
        self.assertIn("Explore.md", out)
        self.crew.land(self.data)
        out = "\n".join(self.crew.check())
        self.assertIn("advisor: none", out)
        self.assertIn("crew files: none", out)

    def test_shell_override_warning(self):
        os.environ["ANTHROPIC_MODEL"] = "sonnet"
        try:
            out = "\n".join(self.crew.takeoff(self.data))
        finally:
            del os.environ["ANTHROPIC_MODEL"]
        self.assertIn("ANTHROPIC_MODEL", out)

    def test_project_override_warning(self):
        proj = os.path.join(self.tmp, "proj", ".claude")
        os.makedirs(proj)
        json.dump({"model": "sonnet"}, open(os.path.join(proj, "settings.json"), "w"))
        out = "\n".join(self.crew.takeoff(self.data, cwd=os.path.dirname(proj)))
        self.assertIn("wins over crew here", out)

    def test_cli_refuses_unsubstituted_data_path(self):
        rc = self.crew.main(["takeoff", "--data", "${CLAUDE_PLUGIN_DATA}"])
        self.assertEqual(rc, 1)
        self.assertNotIn("advisorModel", self.settings())

    def test_existing_empty_model_settings_survives(self):
        self.write({"theme": "dark", "modelSettings": {}})
        before = self.raw()
        self.crew.takeoff(self.data)
        self.crew.land(self.data)
        self.assertEqual(self.raw(), before)

    def test_non_object_parent_stops_and_changes_nothing(self):
        self.write({"modelSettings": "odd"})
        before = self.raw()
        with self.assertRaises(self.crew.Stop):
            self.crew.takeoff(self.data)
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(os.path.join(self.data, "receipt.json")))

    def test_settings_changed_mid_land_stops(self):
        self.crew.takeoff(self.data)
        raw, data = self.crew.read_settings()
        s = self.settings()
        s["modelSettings"]["claude-opus-5-5"]["effortLevel"] = "xhigh"  # changed after land's first read
        self.write(s)
        path = ("modelSettings", "claude-opus-5-5", "effortLevel")
        with self.assertRaises(self.crew.Stop):
            self.crew.write_settings(raw, data, [(path, "high")], expect=[(path, "medium")])
        self.assertEqual(self.settings()["modelSettings"]["claude-opus-5-5"]["effortLevel"], "xhigh")

    def test_explore_appearing_midway_is_not_overwritten(self):
        orig = self.crew.explore_state
        self.crew.explore_state = lambda: "none"  # looked empty at check time...
        open(self.explore(), "w").write("---\nname: Explore\n---\nsomeone else's\n")  # ...then appeared
        before = self.raw()
        try:
            with self.assertRaises(self.crew.Stop):
                self.crew.takeoff(self.data)
        finally:
            self.crew.explore_state = orig
        self.assertIn("someone else's", open(self.explore()).read())
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(os.path.join(self.data, "receipt.json")))
        self.assertFalse(os.path.exists(self.crew.BACKUP))

    def test_lock_blocks_second_command(self):
        with self.crew.Lock(self.data):
            rc = self.crew.main(["takeoff", "--data", self.data])
        self.assertEqual(rc, 1)
        self.assertNotIn("advisorModel", self.settings())

    def test_history_and_keep(self):
        keepdir = os.path.join(self.tmp, "keep")
        self.crew.keep(self.data, keepdir)
        self.crew.takeoff(self.data)
        out = "\n".join(self.crew.land(self.data))
        hist = [json.loads(x) for x in open(os.path.join(self.data, "history.jsonl"))]
        self.assertEqual([h["event"] for h in hist], ["takeoff", "land"])
        self.assertTrue(os.path.exists(os.path.join(keepdir, "history.jsonl")))
        self.assertTrue(os.listdir(os.path.join(keepdir, "landings")))
        self.assertIn("Card saved", out)

    def test_hook_on_and_off(self):
        import subprocess
        hook = os.path.join(HERE, "..", "plugins", "crew", "hooks", "rules.py")
        env = dict(os.environ, CLAUDE_PLUGIN_DATA=self.data)
        r = subprocess.run(["python3", hook], input="{}", capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.crew.takeoff(self.data)
        r = subprocess.run(["python3", hook], input="{}", capture_output=True, text=True, env=env)
        out = json.loads(r.stdout)
        self.assertIn("advisor", out["hookSpecificOutput"]["additionalContext"])
        # Broken data path: still silent, still exit 0.
        env2 = dict(os.environ, CLAUDE_PLUGIN_DATA="/nonexistent/\0bad")
        r = subprocess.run(["python3", hook], input="not json", capture_output=True, text=True,
                           env={k: v for k, v in env2.items() if "\0" not in v})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    def loop(self, command="pytest -q", response=None, event="PostToolUse", session="test-session", raw=None):
        import subprocess
        hook = os.path.join(HERE, "..", "plugins", "crew", "hooks", "loop_watch.py")
        payload = {"hook_event_name": event, "tool_name": "Bash", "session_id": session,
                   "tool_input": {"command": command},
                   "tool_response": response if response is not None else {"exit_code": 1}}
        if event == "PostToolUseFailure":
            payload["error"] = "Exit code 1\nError: test failed"
        result = subprocess.run(["python3", hook], input=raw if raw is not None else json.dumps(payload),
                                capture_output=True, text=True,
                                env=dict(os.environ, CLAUDE_PLUGIN_DATA=self.data))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout else None

    def test_loop_off_and_first_failure_silent(self):
        self.assertIsNone(self.loop())
        self.assertFalse(os.path.exists(os.path.join(self.data, "loops")))
        self.crew.takeoff(self.data)
        self.assertIsNone(self.loop())
        self.crew.land(self.data)
        self.assertIsNone(self.loop())

    def test_loop_twice_fail_nudge_once_and_normalisation(self):
        self.crew.takeoff(self.data)
        self.assertIsNone(self.loop(command='cd "some folder" && pytest   -q 2>&1 | tail -20'))
        out = self.loop()
        self.assertEqual(out["hookEventName"], "PostToolUse")
        self.assertIn("`pytest -q` has failed twice in a row", out["additionalContext"])
        self.assertNotIn("decision", out)
        self.assertIsNone(self.loop())
        self.assertIsNone(self.loop(response={"exit_code": 0}))
        self.assertIsNone(self.loop())
        self.assertIsNone(self.loop())  # once per session, even after a pass
        self.assertIsNone(self.loop(session="other-session"))
        self.assertIsNotNone(self.loop(session="other-session"))

    def test_loop_changed_error_is_progress_not_a_loop(self):
        self.crew.takeoff(self.data)
        a1 = {"exit_code": 1, "stdout": "FAILED test_a.py::test_x - assert 1 == 2\n1 failed in 0.04s"}
        a2 = {"exit_code": 1, "stdout": "FAILED test_a.py::test_x - assert 1 == 2\n1 failed in 0.31s"}
        b = {"exit_code": 1, "stdout": "FAILED test_a.py::test_y - KeyError: 'name'\n1 failed in 0.05s"}
        self.assertIsNone(self.loop(response=a1))
        self.assertIsNone(self.loop(response=b))   # different error: the fix changed something
        self.assertIsNone(self.loop(response=a1))  # changed again
        self.assertIsNotNone(self.loop(response=a2))  # same error, only the timing differs

    def test_loop_pass_resets_consecutive_failures(self):
        self.crew.takeoff(self.data)
        self.assertIsNone(self.loop())
        self.assertIsNone(self.loop(response={"exit_code": 0, "stdout": "failed is just a word"}))
        self.assertIsNone(self.loop())
        self.assertIsNotNone(self.loop())

    def test_loop_failure_event_and_output_fallbacks(self):
        self.crew.takeoff(self.data)
        cases = [("PostToolUseFailure", {}), ("PostToolUse", {"is_error": True}),
                 ("PostToolUse", {"exitCode": 2}), ("PostToolUse", "Exit code 1\nOops"),
                 ("PostToolUse", {"stdout": "2 failed, 1 passed"}),
                 ("PostToolUse", {"stderr": "error TS123: bad type"}),
                 ("PostToolUse", {"stderr": "Error: broken"})]
        for i, (event, response) in enumerate(cases):
            with self.subTest(event=event, response=response):
                kwargs = dict(event=event, response=response, session=f"case-{i}")
                self.assertIsNone(self.loop(**kwargs))
                self.assertEqual(self.loop(**kwargs)["hookEventName"], event)
        self.assertIsNone(self.loop(response={"stdout": "0 failed, 0 errors"}))
        self.assertIsNone(self.loop())

    def test_loop_non_tests_and_junk_silent(self):
        self.crew.takeoff(self.data)
        for command in ("ls", "echo pytest failed", "git status", "cat test.py", "npm install"):
            self.assertIsNone(self.loop(command=command))
            self.assertIsNone(self.loop(command=command))
        for raw in ("bad json", "[]", "null", '{}', '{"tool_input": null}'):
            self.assertIsNone(self.loop(raw=raw))
        self.assertIsNone(self.loop(session="../../escape"))
        self.assertFalse(os.path.exists(os.path.join(self.data, "loops")))

    def test_loop_recognises_runners_and_caps_state(self):
        self.crew.takeoff(self.data)
        commands = ["pytest", "python -m pytest", "python3 -m unittest", "npm test", "pnpm run build",
                    "yarn run test", "node --test", "jest", "vitest", "go test ./...", "cargo test",
                    "cargo build", "make test", "tsc", "ruff check .", "mypy .", "eslint .", "swift test"]
        for command in commands:
            self.assertIsNone(self.loop(command=command))
            self.assertIsNotNone(self.loop(command=command), command)
        for i in range(55):
            self.loop(command=f"pytest test_{i}.py")
        path = os.path.join(self.data, "loops", "test-session.json")
        state = json.load(open(path))
        self.assertEqual(len(state), 50)
        self.assertTrue(state["pytest"]["nudged"])
        open(path, "w").write("broken state")
        self.assertIsNone(self.loop())

    def test_loop_registered_for_both_bash_events(self):
        path = os.path.join(HERE, "..", "plugins", "crew", "hooks", "hooks.json")
        hooks = json.load(open(path))["hooks"]
        for event in ("PostToolUse", "PostToolUseFailure"):
            self.assertEqual(hooks[event][0]["matcher"], "Bash")
            self.assertIn("loop_watch.py", hooks[event][0]["hooks"][0]["command"])

    def test_backup_written_with_undo_and_removed_from_both_locations(self):
        keep = os.path.join(self.tmp, "keep")
        self.crew.keep(self.data, keep)
        self.crew.takeoff(self.data)
        backup = json.load(open(self.crew.BACKUP))
        self.assertEqual(backup["made_by"], "crew")
        self.assertIn('set "model" back to "opus"', backup["undo_by_hand"])
        self.assertIn('remove "advisorModel"', backup["undo_by_hand"])
        self.assertIn('set "modelSettings.claude-opus-5-5.effortLevel" back to "high"', backup["undo_by_hand"])
        self.assertIn("delete " + self.explore(), backup["undo_by_hand"])
        kept = os.path.join(keep, "crew-receipt.json")
        self.assertEqual(json.load(open(kept)), backup)
        self.crew.land(self.data)
        self.assertFalse(os.path.exists(self.crew.BACKUP))
        self.assertFalse(os.path.exists(kept))

    def test_land_recovers_backup_after_entire_data_folder_lost(self):
        before = self.raw()
        keep = os.path.join(self.tmp, "keep")
        self.crew.keep(self.data, keep)
        self.crew.takeoff(self.data)
        shutil.rmtree(self.data)
        self.crew.land(self.data)
        self.assertEqual(self.raw(), before)
        self.assertFalse(os.path.exists(self.explore()))
        self.assertFalse(os.path.exists(self.crew.BACKUP))
        self.assertFalse(os.path.exists(os.path.join(keep, "crew-receipt.json")))

    def test_check_reports_backup_without_plugin_data(self):
        self.crew.takeoff(self.data)
        shutil.rmtree(self.data)
        self.assertIn(self.crew.BACKUP, "\n".join(self.crew.check()))
        self.assertIn("already on", "\n".join(self.crew.takeoff(self.data)))

    def test_foreign_backup_never_deleted_or_used(self):
        keep = os.path.join(self.tmp, "keep")
        self.crew.keep(self.data, keep)
        self.crew.takeoff(self.data)
        foreign = '{"made_by": "someone else", "precious": true}'
        for path in (self.crew.BACKUP, os.path.join(keep, "crew-receipt.json")):
            open(path, "w").write(foreign)
        self.crew.land(self.data)
        for path in (self.crew.BACKUP, os.path.join(keep, "crew-receipt.json")):
            self.assertEqual(open(path).read(), foreign)
        self.assertIn("Crew is off", "\n".join(self.crew.land(self.data)))
        self.assertNotIn(self.crew.BACKUP, "\n".join(self.crew.check()))
        with self.assertRaises(self.crew.Stop):
            self.crew.takeoff(self.data)
        self.assertEqual(open(self.crew.BACKUP).read(), foreign)

    def test_failed_land_retains_backup(self):
        self.crew.takeoff(self.data)
        self.write(None, raw="broken settings")
        with self.assertRaises(self.crew.Stop):
            self.crew.land(self.data)
        self.assertTrue(os.path.exists(self.crew.BACKUP))
        self.assertTrue(os.path.exists(os.path.join(self.data, "receipt.json")))

    def test_helper_guard(self):
        import subprocess
        hook = os.path.join(HERE, "..", "plugins", "crew", "hooks", "helper_guard.py")
        env = dict(os.environ, CLAUDE_PLUGIN_DATA=self.data)

        def run(tool_input, tool="Agent"):
            ev = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input})
            r = subprocess.run(["python3", hook], input=ev, capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0)
            return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout else None

        build = {"description": "Fix parser", "prompt": "Fix the date parser in a.py", "subagent_type": "general-purpose"}
        # Off: silent.
        self.assertIsNone(run(build))
        self.crew.takeoff(self.data)
        # On: line added, other fields kept.
        out = run(build)
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertTrue(out["updatedInput"]["prompt"].startswith("Do not call the advisor tool.\n\nFix the date"))
        self.assertEqual(out["updatedInput"]["description"], "Fix parser")
        self.assertEqual(out["updatedInput"]["subagent_type"], "general-purpose")
        # Already has the line: silent.
        self.assertIsNone(run({"prompt": "Do not call the advisor tool. Explain why X", "subagent_type": "Plan"}))
        # Search on general-purpose (or no type): refused, pointing at Explore.
        for t in ({"prompt": "Find where the advisor rule is defined", "subagent_type": "general-purpose"},
                  {"prompt": "Do not call the advisor tool. Search the repo for advisorModel"},
                  {"description": "Locate config files", "prompt": "Look in the crew folder for json files"}):
            out = run(t)
            self.assertEqual(out["permissionDecision"], "deny", t)
            self.assertIn("Explore", out["permissionDecisionReason"])
        # Search that also asks for an edit: allowed (line added).
        self.assertEqual(run({"prompt": "Find the parser and fix the bug", "subagent_type": "general-purpose"})
                         ["permissionDecision"], "allow")
        # Search on Explore: allowed, line added.
        out = run({"prompt": "Find where the advisor rule is defined", "subagent_type": "Explore"})
        self.assertEqual(out["permissionDecision"], "allow")
        # Other tools and junk input: silent.
        self.assertIsNone(run({"command": "ls"}, tool="Bash"))
        r = subprocess.run(["python3", hook], input="not json", capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
