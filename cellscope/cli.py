"""CLI: python -m cellscope "question" [--user engineer-a] [--out run.json]"""
from __future__ import annotations

import argparse
import json
import sys

from .pipeline import Pipeline


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cellscope", description="Open-weight battery-cell research copilot (via Otari)")
    ap.add_argument("question")
    ap.add_argument("--user", default="engineer-a", help="end-user id for attribution and soft budget")
    ap.add_argument("--out", help="write all stage events + final answer as JSON")
    a = ap.parse_args(argv)
    pipe = Pipeline()
    events = []
    for ev in pipe.run(a.question, a.user):
        events.append(ev)
        if ev["stage"] == "final":
            continue
        if ev["status"] == "start":
            print(f"[{ev['stage']}] ...", file=sys.stderr)
            continue
        line = f"[{ev['stage']}] {ev['status']}"
        if ev.get("model_answered"):
            line += f"  model={ev['model_answered']} upstream={ev.get('upstream_model')} {ev.get('latency_s')}s ${ev.get('cost_usd')}"
        for k in ("trace", "verification", "message", "otari_error_code"):
            if ev.get(k):
                line += f"  {k}={ev[k]}"
        for fb in ev.get("fallbacks") or []:
            line += f"\n    trace: {fb}"
        print(line, file=sys.stderr)
    final = events[-1]
    print(final["answer"])
    print(f"\n-- request cost ${final['request_cost_usd']:.6f} | {a.user} spent ${final['user_spent_usd']:.4f} "
          f"of ${final['user_cap_usd']:.2f} | {final['elapsed_s']}s", file=sys.stderr)
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"events": events, "final": final}, f, indent=1, default=str)
    return 0 if not final.get("refused") else 2
