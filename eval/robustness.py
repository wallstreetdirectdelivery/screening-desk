#!/usr/bin/env python3
"""Robustness check: how many verdicts change if the wording of customer chats is ignored entirely?

Usage: python3 eval/robustness.py <institution folder> [...]

Prose on file is paraphrased from case to case, so a verdict should rest on structure (who paid whom, which
documents exist and whether their figures match), not on a phrase being worded a particular way. Development aid only.
"""
import collections
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hackathon", "system"))
from tmlib import data, detect, engine  # noqa: E402


def run(inst, ignore):
    detect.IGNORE_CHAT_WORDING = ignore
    out = {}
    for case in inst.cases():
        ctx = engine.Context(inst, case)
        detect.run_all(ctx)
        r = engine.decide(ctx, detect.build_rfi)
        out[case.id] = (r["verdict"], r["reason_code"])
    detect.IGNORE_CHAT_WORDING = False
    return out


for folder in sys.argv[1:]:
    inst = data.Institution(folder)
    base, blind = run(inst, False), run(inst, True)
    moved = collections.Counter()
    example = {}
    for cid in base:
        if base[cid] != blind[cid]:
            key = "{}/{} -> {}/{}".format(base[cid][0], base[cid][1], blind[cid][0], blind[cid][1])
            moved[key] += 1
            example.setdefault(key, cid)
    verdict_changes = sum(n for k, n in moved.items() if k.split("/")[0] != k.split("-> ")[1].split("/")[0])
    print("{}: {} of {} cases change when chat wording is ignored ({} change verdict)".format(inst.name, sum(moved.values()), len(base), verdict_changes))
    for key, n in moved.most_common():
        print("   {:4d}  {}   e.g. {}".format(n, key, example[key]))
