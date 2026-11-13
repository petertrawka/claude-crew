#!/usr/bin/env python3
"""crew_flight: the flight log page, the pace line and the landing card.

  crew_flight.py page    --out FILE.html [--from D] [--to D] [--before-from D --before-to D] [--data DIR]
  crew_flight.py pace    [--no-bar]        one line: this week against a normal week, and Fable's share
  crew_flight.py landing --since ISO-TIME  a short card for the time since takeoff (no names or folders)

Reads saved chats only (through crew_tokens). Makes no AI calls. The pace line asks Claude Code for the
real usage bar with `claude -p /usage`, which uses no tokens (`--no-bar` skips it).
Writes only the file named by --out (or prints).
"""
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crew_tokens as ct  # noqa: E402

ORDER = ["opus", "fable", "sonnet", "haiku", "advisor"]
NAMES = {"opus": "Opus", "fable": "Fable", "sonnet": "Sonnet", "haiku": "Haiku", "advisor": "Advisor (Fable)",
         "other": "Other"}
NOT_BAR = ("Price-equivalent = these tokens at list API prices. It is not the weekly usage bar "
           "(the plan doesn't charge per token); use it for proportions.")
# Starting thresholds from the spec, to be replaced by the before-table.
AHEAD_POINTS = 10
FABLE_SHARE = 0.20


def history(data_dir):
    """Takeoff and land times crew recorded (crew's data folder)."""
    out = []
    if not data_dir:
        return out
    p = os.path.join(data_dir, "history.jsonl")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def summarise(d, since=None):
    """Totals per model family (price-equivalent and tokens) plus event counts."""
    price, tokens = {}, {}
    for r in d.replies:
        if since and r["when"] < since:
            continue
        f = r["family"] if r["family"] in ORDER else "other"
        price[f] = price.get(f, 0) + r["price"]
        tokens[f] = tokens.get(f, 0) + r["input"] + r["w5"] + r["w1"] + r["read"] + r["output"]
    adv_calls = 0
    for a in d.advisor:
        if since and a["when"] < since:
            continue
        adv_calls += 1
        price["advisor"] = price.get("advisor", 0) + a["price"]
        tokens["advisor"] = tokens.get("advisor", 0) + a["input"] + a["w5"] + a["w1"] + a["read"] + a["output"]
    _, events = ct.daily(d)
    if since:
        events = [e for e in events if dt.datetime.fromisoformat(e["when"]) >= since]
    count = lambda t: sum(1 for e in events if e["type"] == t)  # noqa: E731
    return {"price": price, "tokens": tokens, "advisor_calls": adv_calls, "boost": count("boost"),
            "handoffs": count("handoff"), "fable_handoffs": count("fable-handoff"), "events": events}


def shares(m):
    tot = sum(m.values())
    return {k: (v / tot if tot else 0) for k, v in m.items()}


# ---------- pace line ----------

def read_bar():
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION")}
    try:
        r = subprocess.run(["claude", "-p", "/usage", "--no-session-persistence", "--setting-sources", "project"],
                           capture_output=True, text=True, timeout=60, env=env, cwd=os.path.expanduser("~"))
    except (OSError, subprocess.SubprocessError):
        return None
    allm = re.search(r"Current week \(all models\):\s*(\d+)% used", r.stdout)
    fab = re.search(r"Current week \(Fable\):\s*(\d+)% used", r.stdout)
    if not allm:
        return None
    return int(allm.group(1)), (int(fab.group(1)) if fab else None)


def pace_data(now=None, bar=True):
    """This week against a normal week, as numbers.

    With the real usage bar (default): the bar's percent against the share of a full week a normal week has used
    by now (the user normally uses the whole plan, so a normal week ends near 100%). Without it: this week's
    price-equivalent against an average full week, which is rougher.
    """
    now = (now or dt.datetime.now().astimezone())
    naive = now.replace(tzinfo=None)
    ws = ct.week_start(naive)
    d = ct.collect((ws - dt.timedelta(days=35)).date(), now.date())
    rows, normal, weeks = ct.pace(d.replies, d.advisor, now=naive)
    if not rows:
        normal = [i / 7 for i in range(1, 8)]
    elapsed = (naive - ws).total_seconds() / 86400
    day = min(7, int(elapsed) + 1)
    prev = normal[day - 2] if day >= 2 else 0.0
    expected = 100 * (prev + (normal[day - 1] - prev) * (elapsed - (day - 1)))
    used = sum(weeks.get(ws, [0.0] * 7))
    fable = sum(r["price"] for r in d.replies if r["family"] == "fable" and r["when"].replace(tzinfo=None) >= ws)
    fable += sum(a["price"] for a in d.advisor if a["family"] == "fable" and a["when"].replace(tzinfo=None) >= ws)
    fshare = 100 * fable / used if used else 0
    b = read_bar() if bar else None
    if b:
        pct_used, fable_bar, estimate = b[0], b[1], False
    else:
        normal_total = sum(r[1] for r in rows) / len(rows) if rows else 0
        pct_used, fable_bar, estimate = (100 * used / normal_total if normal_total else 0), None, True
    ahead = pct_used - expected
    if fshare > 100 * FABLE_SHARE:
        status, act = "act", "ACT TODAY: Fable is over a fifth → /advisor off, shorter chats"
    elif ahead > AHEAD_POINTS:
        status, act = "act", f"ACT TODAY: {ahead:.0f} points ahead → shorter chats, small jobs on Sonnet"
    else:
        status, act = "ok", "On track"
    return {"day": day, "used": pct_used, "estimate": estimate, "normal": expected, "fable_share": fshare,
            "fable_bar": fable_bar, "status": status, "text": act}


def pace_line(now=None, bar=True):
    p = pace_data(now, bar)
    used_txt = f"used about {p['used']:.0f}% (estimate)" if p["estimate"] else f"used {p['used']:.0f}%"
    fable_bar = f" (bar {p['fable_bar']}%)" if p["fable_bar"] is not None else ""
    return (f"Week day {p['day']} of 7 · {used_txt} · normal by now {p['normal']:.0f}% · "
            f"Fable {p['fable_share']:.0f}% of use{fable_bar} · {p['text']}")


# ---------- landing card ----------

def landing(since):
    d = ct.collect(since.date(), dt.date.today())
    s = summarise(d, since=since)
    ps = shares(s["price"])
    ts = shares(s["tokens"])
    span = dt.datetime.now().astimezone() - since
    hours = span.total_seconds() / 3600
    length = (f"{span.total_seconds() / 60:.0f} minutes" if hours < 1 else
              f"{hours:.0f} hours" if hours < 48 else f"{span.days} days")
    lines = [f"Crew landing report: {since:%d %b %H:%M} to now ({length})", ""]
    lines.append("| Model | Share of price-equivalent | Share of tokens |")
    lines.append("|---|---|---|")
    for k in ORDER + ["other"]:
        if k in s["price"] or k in s["tokens"]:
            lines.append(f"| {NAMES[k]} | {100 * ps.get(k, 0):.0f}% | {100 * ts.get(k, 0):.0f}% |")
    lines.append("")
    lines.append(f"Advisor calls: {s['advisor_calls']} · /crew boost: {s['boost']} · handoffs: {s['handoffs']} "
                 f"(to Fable: {s['fable_handoffs']})")
    lines.append(NOT_BAR)
    return "\n".join(lines)


# ---------- the page ----------

DOT = {"advisor": "Advisor call", "boost": "/crew boost", "fable-handoff": "Fable handoff", "handoff": "Handoff"}

CSS = """
:root {
  /* Minimal, in the Vercel / Linear way: flat surfaces, hairline borders, colour only in small marks. */
  --bg: #fafafa; --card: #fff; --hover: #f5f5f5;
  --fg: #171717; --secondary: #666; --tertiary: #a3a3a3;
  --border: #ebebeb; --fill: rgba(0,0,0,.04); --fill-strong: rgba(0,0,0,.08);
  --go: #16a34a; --stop: #dc2626;
  /* Models: Opus neutral (it is most of the bar), the others muted accents, noticed but never shouting. */
  --opus: #3f3f46; --fable: #6b5b95; --sonnet: #4a6a8f; --haiku: #3d6b52; --advisor: #9c4a3e;
  --boost: #9a7a45; --handoff: #a3a3a3;
  --crewon: rgba(61,107,82,.07);
  --shadow: 0 0 0 1px rgba(0,0,0,.06), 0 16px 48px -16px rgba(0,0,0,.14);
  --soft: cubic-bezier(.22,1,.36,1);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1c1c1f; --card: #232326; --hover: #29292d;
    --fg: #ececee; --secondary: #a1a1a8; --tertiary: #707077;
    --border: #333338; --fill: rgba(255,255,255,.05); --fill-strong: rgba(255,255,255,.1);
    --go: #4ade80; --stop: #f87171;
    --opus: #d4d4d8; --fable: #9d8ec4; --sonnet: #7d9cc0; --haiku: #6f9a80; --advisor: #b87468;
    --boost: #b39a6c; --handoff: #707077;
    --crewon: rgba(111,154,128,.10);
    --shadow: 0 0 0 1px #34343a, 0 16px 48px -16px rgba(0,0,0,.7);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.6 -apple-system, BlinkMacSystemFont, "Inter", "SF Pro Text", system-ui, sans-serif;
  font-variant-numeric: tabular-nums; -webkit-font-smoothing: antialiased; }
main { max-width: 960px; margin: 0 auto; padding: 40px 16px 64px; }
/* Spacing in three steps: 8px inside a group, 16px between groups, 24px between sections. */
h1 { margin: 0 0 6px; font-size: 20px; line-height: 1.3; font-weight: 600; letter-spacing: -.02em; }
.meta { margin: 0; font-size: 13px; color: var(--secondary); }
.note { margin: 8px 0 0; font-size: 12px; color: var(--tertiary); }
section { margin-top: 24px; display: grid; gap: 8px; }
h2 { margin: 0; font-size: 13px; line-height: 1.4; font-weight: 500; color: var(--secondary); }
.row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px;
  animation: rise .9s var(--soft) both; animation-delay: calc(var(--i, 0) * 110ms); }
@keyframes rise { from { opacity: 0; transform: translateY(14px) scale(.99); filter: blur(6px); } }

/* Pace: three cells, each a name, a number and a thin line, like the extension's score bento. */
.stats { display: grid; grid-template-columns: repeat(3, 1fr); padding: 0; overflow: hidden; }
.stat { padding: 12px 16px 14px; }
.stat + .stat { border-left: 1px solid var(--border); }
.stat .label { display: block; font-size: 12px; color: var(--secondary); }
.stat .value { display: block; font-size: 22px; font-weight: 600; letter-spacing: -.02em; margin: 2px 0 8px; }
.bar { position: relative; height: 2px; border-radius: 1px; background: var(--fill-strong); overflow: hidden; }
.bar i { position: absolute; inset: 0; background: var(--tertiary); transform: scaleX(0); transform-origin: left;
  animation: fill 1.4s var(--soft) forwards; animation-delay: calc(var(--n, 0) * 220ms + 300ms); }
@keyframes fill { to { transform: scaleX(var(--v)); } }
.bar.fable i { background: var(--fable); }
.status { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--secondary); }
.status::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--tone, var(--go)); flex: none; }
.status.act { --tone: var(--stop); color: var(--fg); font-weight: 500; }

/* Segmented switch, as in the extension. */
.seg { display: inline-flex; padding: 2px; border-radius: 8px; border: 1px solid var(--border); }
.seg button { all: unset; cursor: pointer; padding: 1px 10px; border-radius: 6px; font-size: 12px; color: var(--secondary); }
.seg button[aria-pressed=true] { background: var(--fill-strong); color: var(--fg); }
.seg button:focus-visible { outline: 2px solid var(--fg); outline-offset: 1px; }

.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; color: var(--secondary); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 8px; height: 8px; border-radius: 2px; background: var(--c); }
.legend b { width: 8px; height: 8px; border-radius: 50%; background: var(--c); }
.chart { overflow-x: auto; }
svg { width: 100%; min-width: 560px; height: auto; display: block; }
.lab { font-size: 10px; fill: var(--tertiary); }
.axis { stroke: var(--border); }
.crewon { fill: var(--crewon); }
.seg-opus { fill: var(--opus); } .seg-fable { fill: var(--fable); } .seg-sonnet { fill: var(--sonnet); }
.seg-haiku { fill: var(--haiku); } .seg-advisor { fill: var(--advisor); }
.dot-advisor { fill: var(--advisor); } .dot-boost { fill: var(--boost); } .dot-fable-handoff { fill: var(--fable); }
.dot-handoff { fill: var(--handoff); }
.hit:hover { fill: var(--fill); }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 8px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
td, th { text-align: left; padding: 6px 0; border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: 0; }
th { font-weight: 500; font-size: 12px; color: var(--tertiary); }
td.num, th.num { text-align: right; }
td .sw { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 8px; background: var(--c); }
.share { position: relative; height: 2px; margin-top: 4px; border-radius: 1px; background: var(--fill-strong); }
.share i { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 1px; background: var(--c); }
.small { margin: 12px 0 0; font-size: 12px; color: var(--tertiary); }
@media (max-width: 600px) { .stats { grid-template-columns: 1fr; } .stat + .stat { border-left: 0; border-top: 1px solid var(--border); } }
@media (prefers-reduced-motion: reduce) { .card, .bar i { animation: none; } .bar i { transform: scaleX(var(--v)); } }
"""


def bars_svg(days, metric, crew_periods):
    keys = sorted(days)
    if not keys:
        return "<p class='meta'>No saved chats in this range.</p>"
    vals = []
    for k in keys:
        v = days[k]
        vals.append({f: (v.get(f, 0) if metric == "price" else v.get("tok:" + f, 0)) for f in ORDER})
    top = max(sum(r.values()) for r in vals) or 1
    w, h, pad = 24, 170, 6
    width = len(keys) * w + 44
    out = [f'<svg viewBox="0 -14 {width} {h + 58}" role="img" aria-label="Daily {metric} by model">']
    for (a, b) in crew_periods:
        xs = [i for i, k in enumerate(keys) if a <= k <= b]
        if xs:
            out.append(f'<rect x="{40 + xs[0] * w}" y="0" width="{len(xs) * w}" height="{h}" class="crewon" rx="4"/>')
    for frac in (0.5, 1.0):
        y = h - h * frac
        lab = f"${top * frac:,.0f}" if metric == "price" else ct.fmt_tok(top * frac)
        out.append(f'<line x1="40" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" class="axis" stroke-dasharray="2 4"/>'
                   f'<text x="0" y="{y + 3:.1f}" class="lab">{lab}</text>')
    out.append(f'<line x1="40" y1="{h}" x2="{width}" y2="{h}" class="axis"/>')
    for i, (k, row) in enumerate(zip(keys, vals)):
        x = 40 + i * w + pad / 2
        y = h
        tip = [dt.date.fromisoformat(k).strftime("%a %d %b")]
        segs = [(f, row[f]) for f in ORDER if row[f]]
        for n, (f, v) in enumerate(segs):
            bh = h * v / top
            y -= bh
            rx = 2 if n == len(segs) - 1 else 0
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w - pad}" height="{max(bh, 0.5):.1f}" rx="{rx}" class="seg-{f}"/>')
            tip.append(f"{NAMES[f]}: " + (f"${v:,.2f}" if metric == "price" else ct.fmt_tok(v)))
        out.append(f'<rect x="{x - pad / 2:.1f}" y="0" width="{w}" height="{h}" fill="transparent" class="hit">'
                   f'<title>{html.escape(" · ".join(tip))}</title></rect>')
        if i % 2 == 0 or len(keys) <= 14:
            out.append(f'<text x="{x + (w - pad) / 2:.1f}" y="{h + 16}" class="lab" text-anchor="middle">{int(k[8:])}</text>')
        if k.endswith("-01") or i == 0:
            out.append(f'<text x="{x:.1f}" y="{h + 32}" class="lab">{dt.date.fromisoformat(k):%b}</text>')
    out.append("</svg>")
    return "\n".join(out)


def dots_svg(keys, events):
    if not keys:
        return ""
    w = 24
    width = len(keys) * w + 44
    idx = {k: i for i, k in enumerate(keys)}
    rows = {"advisor": 0, "boost": 1, "fable-handoff": 2, "handoff": 3}
    short = {"advisor": "Advisor", "boost": "Boost", "fable-handoff": "To Fable", "handoff": "Handoff"}
    out = [f'<svg viewBox="0 0 {width} 84" role="img" aria-label="Advisor calls, boosts and handoffs">']
    for t, r in rows.items():
        out.append(f'<text x="0" y="{13 + r * 20}" class="lab">{short[t]}</text>'
                   f'<line x1="40" y1="{10 + r * 20}" x2="{width}" y2="{10 + r * 20}" class="axis" stroke-dasharray="1 5"/>')
    counts = {}
    for e in events:
        k = e["when"][:10]
        if k in idx:
            counts[(k, e["type"])] = counts.get((k, e["type"]), 0) + 1
    for (k, t), n in counts.items():
        cx = 40 + idx[k] * w + (w - 6) / 2 + 3
        out.append(f'<circle cx="{cx:.1f}" cy="{10 + rows[t] * 20}" r="{min(7, 2.5 + n)}" class="dot-{t}">'
                   f'<title>{k}: {n} × {DOT[t]}</title></circle>')
    out.append("</svg>")
    return "\n".join(out)


def share_table(title, s, i=0):
    ps, ts = shares(s["price"]), shares(s["tokens"])
    rows = "".join(
        f"<tr style='--c:var(--{k if k in ORDER else 'tertiary'})'><td><span class='sw'></span>{NAMES[k]}"
        f"<div class='share'><i style='width:{100 * ps.get(k, 0):.1f}%'></i></div></td>"
        f"<td class='num'>{100 * ps.get(k, 0):.0f}%</td><td class='num'>{100 * ts.get(k, 0):.0f}%</td>"
        f"<td class='num'>${s['price'].get(k, 0):,.0f}</td></tr>"
        for k in ORDER + ["other"] if s["price"].get(k) or s["tokens"].get(k))
    return (f"<div class='card' style='--i:{i}'><h2>{html.escape(title)}</h2><table><thead><tr><th>Model</th>"
            f"<th class='num'>Price share</th><th class='num'>Token share</th><th class='num'>Price-eq.</th></tr>"
            f"</thead><tbody>{rows}</tbody></table>"
            f"<p class='small'>Advisor calls {s['advisor_calls']} · /crew boost {s['boost']} · handoffs {s['handoffs']}"
            f" (to Fable {s['fable_handoffs']})</p></div>")


def pace_card(p):
    if not p:
        return "<div class='card' style='--i:0'><p class='meta'>Pace unavailable.</p></div>"
    used_lab = "Used this week" + (" (estimate)" if p["estimate"] else "")
    fable_sub = f" · Fable bar {p['fable_bar']}%" if p["fable_bar"] is not None else ""
    cells = [(used_lab, f"{p['used']:.0f}%", p["used"] / 100, ""),
             ("A normal week by now", f"{p['normal']:.0f}%", p["normal"] / 100, ""),
             ("Fable's share of use", f"{p['fable_share']:.0f}%", p["fable_share"] / 100, "fable")]
    stats = "".join(f"<div class='stat'><span class='label'>{html.escape(l)}</span><span class='value'>{v}</span>"
                    f"<div class='bar {c}'><i style='--v:{min(1, max(0, f)):.3f};--n:{n}'></i></div></div>"
                    for n, (l, v, f, c) in enumerate(cells))
    return (f"<div class='card stats' style='--i:0'>{stats}</div>"
            f"<div class='row'><span class='status {p['status']}'>{html.escape(p['text'])}</span>"
            f"<span class='meta'>Day {p['day']} of 7{fable_sub}</span></div>")


def page(start, end, out, before=None, data_dir=None):
    d = ct.collect(start, end)
    days, events = ct.daily(d)
    k = start
    while k <= end:
        days.setdefault(k.isoformat(), {})
        k += dt.timedelta(days=1)
    hist = history(data_dir)
    periods, on = [], None
    for h in hist:
        if h.get("event") == "takeoff":
            on = h["when"][:10]
        elif h.get("event") == "land" and on:
            periods.append((on, h["when"][:10]))
            on = None
    if on:
        periods.append((on, end.isoformat()))
    cards = [share_table(f"{start:%d %b} to {end:%d %b}", summarise(d), 2)]
    if before:
        db = ct.collect(before[0], before[1])
        cards.insert(0, share_table(f"Before crew: {before[0]:%d %b} to {before[1]:%d %b}", summarise(db), 1))
    try:
        p = pace_data()
    except Exception:  # the page still renders without it
        p = None
    legend = "".join(f"<span style='--c:var(--{f})'><i></i>{NAMES[f]}</span>" for f in ORDER)
    dleg = "".join(f"<span style='--c:var(--{c})'><b></b>{n}</span>" for c, n in
                   (("advisor", "Advisor call"), ("boost", "/crew boost"), ("fable", "Fable handoff"), ("handoff", "Handoff")))
    shaded = " Shaded days: crew was on." if periods else ""
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crew flight log</title>
<style>{CSS}</style></head><body><main>
<header>
<h1>Crew flight log</h1>
<p class="meta">{start:%d %b} to {end:%d %b %Y} · from the saved chats on this Mac · built {dt.datetime.now():%d %b %H:%M}</p>
<p class="note">{html.escape(NOT_BAR)}</p>
</header>
<section><h2>This week</h2>{pace_card(p)}</section>
<section>
<div class="row"><h2>Each day, by model</h2>
<div class="seg" role="group" aria-label="Measure"><button aria-pressed="true" onclick="show('price',this)">Price-eq.</button><button aria-pressed="false" onclick="show('tokens',this)">Tokens</button></div></div>
<div class="card chart" style="--i:1"><div id="m-price">{bars_svg(days, "price", periods)}</div>
<div id="m-tokens" hidden>{bars_svg(days, "tokens", periods)}</div>
{dots_svg(sorted(days), events)}</div>
<div class="legend">{legend}</div>
<div class="legend">{dleg}</div>
<p class="note">Hover a bar for the day's numbers.{shaded}</p>
</section>
<section><h2>Share by model</h2><div class="cards">{"".join(cards)}</div></section>
</main>
<script>
function show(m, b) {{
  for (const x of ['price','tokens']) document.getElementById('m-'+x).hidden = x !== m;
  for (const btn of document.querySelectorAll('.seg button')) btn.setAttribute('aria-pressed', btn === b);
}}
</script></body></html>
"""
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    return out


def arg(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "pace":
        print(pace_line(bar="--no-bar" not in argv))
        return 0
    if cmd == "landing":
        since = arg(argv, "--since")
        if not since:
            print("landing needs --since")
            return 2
        print(landing(dt.datetime.fromisoformat(since).astimezone()))
        return 0
    if cmd == "page":
        out = arg(argv, "--out")
        if not out:
            print("page needs --out FILE.html")
            return 2
        end = dt.date.fromisoformat(arg(argv, "--to")) if arg(argv, "--to") else dt.date.today()
        start = dt.date.fromisoformat(arg(argv, "--from")) if arg(argv, "--from") else end - dt.timedelta(days=27)
        before = None
        if arg(argv, "--before-from") and arg(argv, "--before-to"):
            before = (dt.date.fromisoformat(arg(argv, "--before-from")), dt.date.fromisoformat(arg(argv, "--before-to")))
        print("Saved", page(start, end, out, before, arg(argv, "--data")))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
