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


if __name__ == "__main__":
    unittest.main()
