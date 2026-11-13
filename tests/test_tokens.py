"""Tests for crew_tokens.py on a fake projects folder. Run: python3 -m unittest crew/tests/test_tokens.py"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "plugins", "crew", "scripts"))


def asst(mid, model, ts, cwd, sid, inp=10, read=1000, w1=100, out=50, advisor=None, tool=None):
    it = [{"type": "message", "input_tokens": inp, "output_tokens": out}]
    if advisor:
        it.append({"type": "advisor_message", "model": advisor, "input_tokens": 40000, "output_tokens": 500,
                   "cache_read_input_tokens": 0, "cache_creation": {}})
    content = [{"type": "text", "text": "ok"}]
    if tool:
        content.append({"type": "tool_use", "id": tool[0], "name": "Bash", "input": {"command": tool[1]}})
    return {"type": "assistant", "timestamp": ts, "cwd": cwd, "sessionId": sid, "entrypoint": "cli",
            "message": {"id": mid, "model": model, "content": content,
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": w1,
                                  "cache_creation": {"ephemeral_1h_input_tokens": w1, "ephemeral_5m_input_tokens": 0},
                                  "iterations": it}}}


def user(ts, cwd, sid, text=None, fail_id=None):
    if fail_id:
        content = [{"type": "tool_result", "tool_use_id": fail_id, "is_error": True, "content": "boom"}]
    else:
        content = text
    return {"type": "user", "timestamp": ts, "cwd": cwd, "sessionId": sid, "message": {"role": "user", "content": content}}


class TokensTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CREW_PROJECTS"] = self.tmp
        import importlib
        import crew_tokens
        self.ct = importlib.reload(crew_tokens)
        cwd = "/Users/x/Documents/VS Code Projects/alpha"
        p = os.path.join(self.tmp, "-alpha")
        os.makedirs(os.path.join(p, "s1", "subagents"))
        lines = [
            user("2026-11-06T10:00:00Z", cwd, "s1", "Build ticket 03 please"),
            asst("m1", "claude-opus-5-5", "2026-11-06T10:00:05Z", cwd, "s1", tool=("t1", "npm test")),
            asst("m1", "claude-opus-5-5", "2026-11-06T10:00:05Z", cwd, "s1", out=80),  # same message again, final count
            user("2026-11-06T10:00:06Z", cwd, "s1", fail_id="t1"),
            asst("m2", "claude-opus-5-5", "2026-11-06T10:00:09Z", cwd, "s1", tool=("t2", "npm test"), advisor="claude-fable-5-2"),
            user("2026-11-06T10:00:10Z", cwd, "s1", fail_id="t2"),
            user("2026-11-06T10:01:00Z", cwd, "s1", "<command-name>/crew</command-name>\n<command-args>boost</command-args>"),
            user("2026-11-06T10:02:00Z", cwd, "s1", "<local-command-stdout>x</local-command-stdout>"),
        ]
        with open(os.path.join(p, "s1.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        with open(os.path.join(p, "s1", "subagents", "agent-a1.jsonl"), "w") as f:
            f.write(json.dumps(asst("h1", "claude-haiku-5-5", "2026-11-06T10:00:07Z", cwd, "s1")) + "\n")
        json.dump({"agentType": "Explore", "description": "find files"},
                  open(os.path.join(p, "s1", "subagents", "agent-a1.meta.json"), "w"))
        # A test folder that must be skipped.
        q = os.path.join(self.tmp, "-scratch")
        os.makedirs(q)
        with open(os.path.join(q, "s9.jsonl"), "w") as f:
            f.write(json.dumps(asst("z1", "claude-opus-5-5", "2026-11-06T10:00:00Z", "/tmp/x/scratchpad/t/work", "s9")) + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)
        os.environ.pop("CREW_PROJECTS", None)

    def test_collect(self):
        d = self.ct.collect(dt.date(2026, 11, 1), dt.date(2026, 11, 11))
        self.assertEqual(len(d.replies), 3)  # m1 once, m2, h1; scratch skipped
        self.assertEqual(sorted(r["output"] for r in d.replies if r["family"] == "opus"), [50, 80])  # m1 uses its last version
        fams = sorted(r["family"] for r in d.replies)
        self.assertEqual(fams, ["haiku", "opus", "opus"])
        self.assertEqual(len(d.advisor), 1)
        self.assertEqual(d.advisor[0]["family"], "fable")
        self.assertAlmostEqual(d.advisor[0]["price"], 40000 * 8e-6 + 500 * 40e-6)
        s = d.sessions["s1"]
        self.assertEqual(s["project"], "alpha")
        self.assertEqual(s["kind"], "ticket")
        self.assertEqual(len(s["user"]), 1)  # the command and stdout are not the user's words
        self.assertEqual(s["commands"][0][1], "crew boost")
        self.assertEqual(self.ct.repeat_fails(s), 1)
        kinds = sorted(r["kind"] for r in d.replies)
        self.assertEqual(kinds, ["helper", "ticket-main", "ticket-main"])
        days, events = self.ct.daily(d)
        types = sorted(e["type"] for e in events)
        self.assertEqual(types, ["advisor", "boost"])

    def test_fable_split(self):
        cwd = "/Users/x/Documents/VS Code Projects/beta"
        p = os.path.join(self.tmp, "-beta")
        os.makedirs(os.path.join(p, "s2", "subagents"))
        with open(os.path.join(p, "s2.jsonl"), "w") as f:
            f.write(json.dumps(user("2026-11-07T09:00:00Z", cwd, "s2", "hello")) + "\n")
            f.write(json.dumps(asst("f1", "claude-fable-5-2", "2026-11-07T09:00:05Z", cwd, "s2")) + "\n")
        sub = os.path.join(p, "s2", "subagents")
        for name, mid, model, meta, adv in (
                ("agent-b1", "f2", "claude-fable-5-2", {"agentType": "general-purpose", "model": "fable",
                                                        "description": "Fable review"}, None),
                ("agent-b2", "f3", "claude-fable-5-2", {"agentType": "general-purpose", "description": "help"}, None),
                ("agent-b3", "o1", "claude-opus-5-5", {"agentType": "general-purpose", "description": "Run ticket 02"},
                 "claude-fable-5-2")):
            with open(os.path.join(sub, name + ".jsonl"), "w") as f:
                f.write(json.dumps(asst(mid, model, "2026-11-07T09:01:00Z", cwd, "s2", advisor=adv)) + "\n")
            json.dump(meta, open(os.path.join(sub, name + ".meta.json"), "w"))
        d = self.ct.collect(dt.date(2026, 11, 1), dt.date(2026, 11, 11))
        split = self.ct.fable_split(d)
        self.assertEqual({k: v[0] for k, v in split.items()},
                         {"main": 1, "asked": 1, "inherited": 1, "advisor-main": 1, "advisor-helper": 1})
        fable_total = (sum(r["price"] for r in d.replies if r["family"] == "fable")
                       + sum(a["price"] for a in d.advisor if a["family"] == "fable"))
        self.assertAlmostEqual(sum(v[1] for v in split.values()), fable_total)
        text = self.ct.report(dt.date(2026, 11, 1), dt.date(2026, 11, 11))[0]
        self.assertIn("## Who picked Fable", text)
        self.assertIn("| Helper started on Fable |", text)

    def test_price(self):
        # Haiku: matches Claude Code's own figure for test t1 ($0.00531711).
        self.assertAlmostEqual(self.ct.price("haiku", 2, 0, 25650, 11391, 146), 0.00531711, places=6)

    def test_describe(self):
        s = {"user": [(None, "a")], "fails": {}, "replies": 3}
        self.assertEqual(self.ct.describe(s, {"corrections": 0}), "Short and clean")
        self.assertEqual(self.ct.describe(s, {"corrections": 2}), "Struggled")
        s2 = {"user": [(None, "a")] * 6, "fails": {}, "replies": 40}
        self.assertEqual(self.ct.describe(s2, {"corrections": 1}), "Smooth")
        self.assertEqual(self.ct.describe(s2, {"corrections": 0, "reopen": True}), "Struggled")
        s3 = {"user": [(None, "a")], "fails": {"Bash:npm test": 2}, "replies": 3}
        self.assertEqual(self.ct.describe(s3, None), "Struggled")
        self.assertIsNone(self.ct.describe(s, None))

    def test_week_start_across_clock_change(self):
        a = self.ct.week_start(dt.datetime(2026, 10, 24, 12, 0))  # Saturday before the change
        b = self.ct.week_start(dt.datetime(2026, 10, 27, 12, 0))  # Tuesday after
        self.assertEqual(a, b)
        self.assertEqual(a, dt.datetime(2026, 10, 22, 3, 0))
        self.assertEqual(self.ct.week_start(dt.datetime(2026, 10, 22, 2, 59)), dt.datetime(2026, 10, 15, 3, 0))

    def test_partial_week_at_range_end_not_counted(self):
        base = dict(session="s", project="p", kind="main", family="opus", price=1.0)
        reps = [dict(base, when=dt.datetime(2026, 10, 15, 4) + dt.timedelta(days=i)) for i in range(7)]
        reps.append(dict(base, when=dt.datetime(2026, 10, 22, 4)))  # Thursday only of the next week
        rows, normal, _ = self.ct.pace(reps, [], now=dt.datetime(2026, 10, 23, 0))
        self.assertEqual(len(rows), 1)

    def test_clean_for_haiku(self):
        t = self.ct.clean_for_haiku("see <pasted_content id=1>SECRET</pasted_content> key sk-ant-abcdefgh123 "
                                    "AbCdEf1234567890GhIjKlMnOp and handoff/2026-10-21-1530-topic.md ```code```")
        self.assertNotIn("SECRET", t)
        self.assertNotIn("sk-ant", t)
        self.assertNotIn("AbCdEf", t)
        self.assertIn("2026-10-21-1530-topic", t)
        self.assertIn("[code]", t)


if __name__ == "__main__":
    unittest.main()
