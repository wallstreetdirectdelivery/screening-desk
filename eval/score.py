#!/usr/bin/env python3
"""Check a system output file and, where hand labels exist, score it against them.

Usage:
    python3 -I eval/score.py <institution folder> <output file> [<labels file>]

Part 1 validates the output format for every case in the folder (this needs no labels).
Part 2 scores the labelled subset with the hackathon cost table.

This is a development aid. It is not part of the submitted system and the system never reads it.
"""
import collections
import json
import os
import sys

CODES = {
    "CLOSE": {"COLLECTION_DOCUMENTED", "BUSINESS_RECORDS_RECONCILE", "DOCUMENTED_EVENT", "INVOICE_MATCHES",
              "OWN_ACCOUNT", "GROUP_COMPANY", "THIRD_PARTY_DOCUMENTED", "REFUND_OF_EARLIER_PAYMENT",
              "RETURNING_CUSTOMER", "LIST_MATCH_DIFFERENT_PARTY", "WITHIN_PROFILE"},
    "RFI": {"RFI_PURPOSE_DOCUMENTS", "RFI_BUSINESS_RECORDS", "RFI_SOURCE_OF_FUNDS", "RFI_COUNTERPARTY_DETAILS",
            "RFI_UPDATED_PROFILE"},
    "ESCALATE": {"ESC_MULE_ACTIVITY", "ESC_STRUCTURING", "ESC_UNEXPLAINED_FUNDS", "ESC_PASS_THROUGH",
                 "ESC_INVOICE_MISMATCH", "ESC_HIGH_RISK_CORRIDOR", "ESC_ACCOUNT_MISUSE", "ESC_THIRD_PARTY_FUNDING",
                 "ESC_EARLY_REDEMPTION", "ESC_LISTED_PARTY"},
}
# COST[true verdict][sent verdict]
COST = {
    "CLOSE": {"CLOSE": 0, "RFI": 3, "ESCALATE": 3},
    "RFI": {"CLOSE": 10, "RFI": 0, "ESCALATE": 2},
    "ESCALATE": {"CLOSE": 10, "RFI": 3, "ESCALATE": 0},
}


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def validate(folder, rows):
    cases = {c["case_id"]: c for c in load_jsonl(os.path.join(folder, "cases.jsonl"))}
    known = collections.defaultdict(set)  # customer_id -> IDs that belong to that customer's case files
    for t in load_jsonl(os.path.join(folder, "transactions.jsonl")):
        known[t["customer_id"]].add(t["transaction_id"])
    for e in load_jsonl(os.path.join(folder, "evidence.jsonl")):
        known[e["customer_id"]].add(e["doc_id"])
    for r in load_jsonl(os.path.join(folder, "previous_reviews.jsonl")):
        known[r["customer_id"]].add(r["review_id"])
    list_ids = {e["entry_id"] for e in load_jsonl(os.path.join(folder, "internal_list.jsonl"))}
    alert_ids = collections.defaultdict(set)
    for a in load_jsonl(os.path.join(folder, "alerts.jsonl")):
        alert_ids[a["case_id"]].add(a["alert_id"])

    problems = collections.Counter()
    examples = {}

    def bad(kind, case_id, detail=""):
        problems[kind] += 1
        examples.setdefault(kind, "{} {}".format(case_id, detail).strip())

    seen = collections.Counter(r.get("case_id") for r in rows)
    for cid in cases:
        if seen[cid] == 0:
            bad("case missing from output", cid)
        elif seen[cid] > 1:
            bad("case appears more than once", cid)
    for r in rows:
        cid = r.get("case_id")
        if cid not in cases:
            bad("unknown case_id", cid)
            continue
        if set(r.get("alert_ids") or []) != set(cases[cid]["alert_ids"]):
            bad("alert_ids do not match the case", cid)
        v = r.get("verdict")
        if v not in CODES:
            bad("invalid verdict", cid, str(v))
        elif r.get("reason_code") not in CODES[v]:
            bad("reason_code does not belong to the verdict", cid, "{}/{}".format(v, r.get("reason_code")))
        reason = r.get("reason") or ""
        if len(reason) < 60:
            bad("reason shorter than 60 characters", cid)
        ev = r.get("evidence") or []
        if not ev:
            bad("no evidence cited", cid)
        ok = known[cases[cid]["customer_id"]] | list_ids | alert_ids[cid]
        for e in ev:
            if e not in ok:
                bad("cited ID not in the case's files", cid, e)
    return problems, examples


def score(rows, labels):
    out = {r["case_id"]: r for r in rows}
    confusion = collections.Counter()
    cost = base = 0
    code_ok = ev_hit = right = 0
    wrongful, misses = [], []
    for lab in labels:
        truth = lab["verdict"]
        r = out.get(lab["case_id"])
        sent = r["verdict"] if r and r.get("verdict") in CODES else "ESCALATE"
        confusion[(truth, sent)] += 1
        cost += COST[truth][sent]
        base += COST[truth]["ESCALATE"]
        if sent == truth:
            right += 1
            if r and r.get("reason_code") == lab["reason_code"]:
                code_ok += 1
            docs = [e for e in lab.get("evidence", []) if "-T" not in e]
            if r and docs and set(docs) & set(r.get("evidence") or []):
                ev_hit += 1
        else:
            if sent == "CLOSE":
                wrongful.append((lab["case_id"], truth, lab.get("confidence")))
            misses.append((lab["case_id"], truth, sent, lab.get("confidence")))
    n = len(labels)
    print("\nScored against {} labelled cases".format(n))
    print("  verdict accuracy: {}/{} ({:.0%})".format(right, n, right / n if n else 0))
    print("  cost per case: {:.2f}   (escalate everything: {:.2f})".format(cost / n if n else 0, base / n if n else 0))
    print("  verdict score (share of escalate-everything cost saved): {:.1%}".format(1 - cost / base if base else 0))
    print("  wrongful closes: {}".format(len(wrongful)))
    truly_close = sum(v for (t, s), v in confusion.items() if t == "CLOSE")
    print("  closable cases closed: {}/{}".format(confusion[("CLOSE", "CLOSE")], truly_close))
    print("  of correct verdicts: reason code matches {}/{}, a deciding document is cited {}/{}".format(
        code_ok, right, ev_hit, right))
    print("  confusion (rows = label, columns = sent):")
    print("            {:>8} {:>8} {:>8}".format("CLOSE", "RFI", "ESCALATE"))
    for t in ("CLOSE", "RFI", "ESCALATE"):
        print("  {:9s} {:>8} {:>8} {:>8}".format(t, *(confusion[(t, s)] for s in ("CLOSE", "RFI", "ESCALATE"))))
    for cid, truth, conf in wrongful:
        print("  WRONGFUL CLOSE {} (label {}, label confidence {})".format(cid, truth, conf))
    for cid, truth, sent, conf in misses:
        if sent != "CLOSE":
            print("  miss {}: label {} (confidence {}), sent {}".format(cid, truth, conf, sent))


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    folder, out_path = argv[1], argv[2]
    rows = load_jsonl(out_path)
    problems, examples = validate(folder, rows)
    print("Format check: {} rows".format(len(rows)))
    if not problems:
        print("  no problems")
    for kind, n in problems.most_common():
        print("  {} x {}   e.g. {}".format(n, kind, examples[kind]))
    if len(argv) > 3 and os.path.exists(argv[3]):
        score(rows, load_jsonl(argv[3]))


if __name__ == "__main__":
    main(sys.argv)
