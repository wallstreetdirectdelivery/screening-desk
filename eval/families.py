#!/usr/bin/env python3
"""Show how a system output file distributes over scenario families (rules fired | document types on file).

Usage:
    python3 -I eval/families.py <institution folder> <output file> [<labels file>]

A family whose cases split across verdicts is where to look for mistakes. Development aid only.
"""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import show_case  # noqa: E402


def main(argv):
    inst = show_case.Institution(argv[1])
    rows = {}
    with open(argv[2], encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                rows[r["case_id"]] = r
    labels = collections.defaultdict(set)
    if len(argv) > 3 and os.path.exists(argv[3]):
        with open(argv[3], encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line)
                    labels[inst.signature(r["case_id"])].add(r["verdict"][:3] + "/" + r["reason_code"])
    fam = collections.defaultdict(collections.Counter)
    example = {}
    for cid in inst.cases:
        r = rows.get(cid)
        key = (r["verdict"][:3] + "/" + r["reason_code"]) if r else "MISSING"
        sig = inst.signature(cid)
        fam[sig][key] += 1
        example.setdefault((sig, key), cid)
    totals = collections.Counter(r["verdict"] for r in rows.values())
    print("verdicts:", dict(totals))
    for sig in sorted(fam):
        want = "; ".join(sorted(labels[sig])) if labels[sig] else "-"
        got = fam[sig]
        flag = "  " if set(got) <= labels[sig] or not labels[sig] else "!!"
        print("{} {:4d} {}".format(flag, sum(got.values()), sig))
        print("          label: {}".format(want))
        for key, n in got.most_common():
            print("          {:4d} {:34s} e.g. {}".format(n, key, example[(sig, key)]))


if __name__ == "__main__":
    main(sys.argv)
