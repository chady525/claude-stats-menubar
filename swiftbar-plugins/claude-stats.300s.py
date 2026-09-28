#!/opt/homebrew/bin/python3.14
# <swiftbar.title>Claude Code Usage</swiftbar.title>
# <swiftbar.version>7.0</swiftbar.version>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
"""Menu bar meter that mirrors `/usage` inside Claude Code.

Two slim bars fill left to right:

    bright  →  current 5-hour session window
    dim     →  weekly limit (all models)

Each bar is orange, turns amber past 85% and red at 100%. The session
percentage sits beside them in the menu-bar font. The dropdown repeats both
limits with reset times, then a rough local token/cost estimate for the last
5h broken down per model, with subagent usage called out separately.
"""

import base64
import datetime
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

CLAUDE_DIR = os.path.expanduser("~/.claude/projects")
CACHE = os.path.expanduser("~/Library/Caches/claude-stats-swiftbar.txt")
MAX_CACHE_AGE = 30 * 60  # still refresh when idle, so session resets show up

# Battery: when no transcript changed since the last refresh, usage can't have
# gone up — reprint the cached output and skip the `claude` spawn + parse.
if "--force" not in sys.argv:
    try:
        cached_at = os.path.getmtime(CACHE)
        last_write = max(
            (os.path.getmtime(p) for p in glob.glob(os.path.join(CLAUDE_DIR, "**/*.jsonl"), recursive=True)),
            default=0,
        )
        if last_write < cached_at and time.time() - cached_at < MAX_CACHE_AGE:
            sys.stdout.write(open(CACHE, encoding="utf-8").read())
            sys.exit(0)
    except OSError:
        pass

from PIL import Image, ImageDraw  # noqa: E402 — only needed on a real refresh
WINDOW_HOURS = 5

# $ per million tokens, by model family. Public list prices; cache-write is the
# 5-minute rate. Used only for the dropdown's rough estimate.
PRICE = {
    "opus":   {"in": 15.0, "out": 75.0, "cr": 1.50, "cw": 18.75},
    "sonnet": {"in": 3.0,  "out": 15.0, "cr": 0.30, "cw": 3.75},
    "haiku":  {"in": 1.0,  "out": 5.0,  "cr": 0.10, "cw": 1.25},
}
_DEFAULT_PRICE = PRICE["sonnet"]
_FAMILY_NAME = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "other": "Other"}

ORANGE = "#F0821E"
AMBER = "#FF9F0A"
RED = "#FF453A"

_USAGE_LINE = re.compile(
    r"Current\s+(session|week[^:]*):\s*(\d+)%\s*used\s*(?:·\s*resets\s*(.+))?",
    re.I,
)


def fmt_k(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}k"
    return str(n)


def model_family(model):
    m = (model or "").lower()
    for fam in ("opus", "haiku", "sonnet"):
        if fam in m:
            return fam
    return "other"


def level_hex(pct):
    if pct is None:
        return ORANGE
    return RED if pct >= 100 else AMBER if pct >= 85 else ORANGE


def find_claude():
    for cand in (
        shutil.which("claude"),
        os.path.expanduser("~/.local/bin/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ):
        if cand and os.path.exists(cand):
            return cand
    return "claude"


def cleanup_usage_junk():
    """Delete the throwaway transcripts each `claude -p "/usage"` call leaves."""
    for path in glob.glob(os.path.join(CLAUDE_DIR, "*", "*.jsonl")):
        try:
            if os.path.getsize(path) > 20_000:
                continue
            with open(path, encoding="utf-8") as f:
                if '"content":"/usage"' not in f.read(4000):
                    continue
            os.remove(path)
            proj = os.path.dirname(path)
            mem = os.path.join(proj, "memory")
            if os.path.isdir(mem) and not os.listdir(mem):
                os.rmdir(mem)
            if not os.listdir(proj):
                os.rmdir(proj)
        except OSError:
            continue


def get_usage_limits():
    """Live `/usage` limits: [{label, pct, resets}], or [] on failure."""
    try:
        out = subprocess.run(
            [find_claude(), "-p", "/usage", "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=45,
        ).stdout
    except Exception:
        return []
    finally:
        cleanup_usage_junk()
    return [
        {"label": lbl.strip().rstrip(":"), "pct": int(pct), "resets": (r or "").strip().rstrip(".")}
        for lbl, pct, r in _USAGE_LINE.findall(out)
    ]


def _blank_bucket():
    return dict(input=0, output=0, cache_read=0, cache_creation=0, messages=0, cost=0.0)


def get_window_stats():
    """Rough local token totals for the last WINDOW_HOURS.

    Returns per-model buckets plus a `subagents` bucket (sidechain turns, which
    are also counted in their model bucket). Deduped by message id across the
    resumed / branched transcript copies.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    ws = now - datetime.timedelta(hours=WINDOW_HOURS)
    by_model = {}
    sub = _blank_bucket()
    seen = set()
    ws_epoch = ws.timestamp()

    for path in glob.glob(os.path.join(CLAUDE_DIR, "**/*.jsonl"), recursive=True):
        try:
            # Transcripts are append-only: untouched since the window opened
            # means nothing in the window, so skip without reading.
            if os.path.getmtime(path) < ws_epoch:
                continue
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    ts = d.get("timestamp", "")
                    if not ts:
                        continue
                    try:
                        mt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if mt < ws:
                        continue
                    msg = d.get("message", {})
                    u = msg.get("usage")
                    if not u:
                        continue
                    mid = msg.get("id") or d.get("requestId")
                    if mid is not None:
                        if mid in seen:
                            continue
                        seen.add(mid)

                    fam = model_family(msg.get("model"))
                    pr = PRICE.get(fam, _DEFAULT_PRICE)
                    vals = (
                        u.get("input_tokens", 0),
                        u.get("output_tokens", 0),
                        u.get("cache_read_input_tokens", 0),
                        u.get("cache_creation_input_tokens", 0),
                    )
                    c = (
                        vals[0] * pr["in"] + vals[1] * pr["out"]
                        + vals[2] * pr["cr"] + vals[3] * pr["cw"]
                    ) / 1_000_000

                    for bucket in (by_model.setdefault(fam, _blank_bucket()),
                                   sub if d.get("isSidechain") else None):
                        if bucket is None:
                            continue
                        bucket["input"] += vals[0]
                        bucket["output"] += vals[1]
                        bucket["cache_read"] += vals[2]
                        bucket["cache_creation"] += vals[3]
                        bucket["messages"] += 1
                        bucket["cost"] += c
        except Exception:
            continue

    return {"by_model": by_model, "subagents": sub}


def _clamp01(p):
    return max(0.0, min(1.0, p))


def make_capsule(session_pct, week_pct) -> str:
    """Two slim progress bars — session over week, macOS battery-icon style.

    Plain white fill on a faint track, no colour-coding. Rendered as a
    template image so macOS tints it like the native battery/menu-bar icons
    (auto light/dark). Rendered at high scale, downsampled to a @3x asset.
    Tunables: `w_pt`, `bar_h`, `gap`.
    """
    s = 64
    w_pt, bar_h, gap = 7, 1.2, 1.4
    cw, ch = round((w_pt + 2) * s), 12 * s
    x = 1 * s
    span = w_pt * s
    top = (ch - (2 * bar_h + gap) * s) / 2

    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def row(pct, y, alpha):
        y0, y1 = y, y + bar_h * s
        r = bar_h * s / 2
        d.rounded_rectangle([x, y0, x + span, y1], radius=r, fill=(255, 255, 255, 38))
        fw = span * _clamp01((pct or 0) / 100)
        if fw >= 1:
            d.rounded_rectangle([x, y0, x + max(fw, 2 * r), y1], radius=r,
                                fill=(255, 255, 255, alpha))

    row(session_pct, top, 255)
    row(week_pct, top + (bar_h + gap) * s, 165)

    img = img.resize((round(cw * 3 / s), round(ch * 3 / s)), Image.LANCZOS)  # -> @3x
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ── data ─────────────────────────────────────────────────────────────────────
real_stdout, sys.stdout = sys.stdout, io.StringIO()
limits = get_usage_limits()
by = {}
for x in limits:
    low = x["label"].lower()
    key = "session" if low.startswith("session") else ("opus" if "opus" in low else "week")
    by[key] = x

session = by.get("session")
week = by.get("week")
stats = get_window_stats()
by_model = stats["by_model"]
sub = stats["subagents"]

total_cost = sum(m["cost"] for m in by_model.values())
total_msgs = sum(m["messages"] for m in by_model.values())
total_out = sum(m["output"] for m in by_model.values())

# ── SwiftBar output ──────────────────────────────────────────────────────────
capsule = make_capsule(session["pct"] if session else None, week["pct"] if week else None)
label = f"{session['pct']}%" if session else "–"
print(f"{label} | image={capsule} templateImage=true size=13")
print("---")

if limits:
    if session:
        print(f"Session   {session['pct']}%   ·   resets {session['resets']} "
              f"| size=13 color={level_hex(session['pct'])}")
    if week:
        print(f"Week   {week['pct']}%   ·   resets {week['resets']} "
              f"| size=13 color={level_hex(week['pct'])}")
    opus = by.get("opus")
    if opus:
        print(f"Opus   {opus['pct']}%   ·   resets {opus['resets']} "
              f"| size=13 color={level_hex(opus['pct'])}")
else:
    print("/usage unavailable — is `claude` logged in? | size=13")

print("---")
print("Last 5h · local estimate | size=12 color=#8E8E93")
print(f"{total_msgs} messages   ·   {fmt_k(total_out)} out   ·   ~${total_cost:.2f} | size=12")
for fam, m in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"]):
    tok = m["input"] + m["output"] + m["cache_read"] + m["cache_creation"]
    print(f"  {_FAMILY_NAME.get(fam, fam)}   {fmt_k(tok)} tok   ·   {m['messages']} msg   ·   "
          f"~${m['cost']:.2f} | size=12 font=Menlo color=#8E8E93")
if sub["messages"]:
    tok = sub["input"] + sub["output"] + sub["cache_read"] + sub["cache_creation"]
    print(f"  ↳ subagents   {fmt_k(tok)} tok   ·   {sub['messages']} msg   ·   "
          f"~${sub['cost']:.2f} | size=12 font=Menlo color=#8E8E93")
print("---")
print(f"Refresh | bash='{os.path.abspath(__file__)}' param1=--force terminal=false refresh=true sfimage=arrow.clockwise")

out, sys.stdout = sys.stdout.getvalue(), real_stdout
sys.stdout.write(out)
# Written after get_usage_limits() cleaned up its own /usage transcript, so that
# write doesn't count as activity next run.
with open(CACHE, "w", encoding="utf-8") as f:
    f.write(out)
