#!/usr/bin/env python3
"""Render development cases as one readable page each, for hand-labelling and debugging.

Usage:
    python3 -I eval/show_case.py <institution folder> <case_id> [<case_id> ...]
    python3 -I eval/show_case.py <institution folder> --ids-file <file with one case_id per line>

This is a development aid. It is not part of the submitted system and the system never reads it.
"""
import collections
import json
import os
import sys

BASELINE = {"due_diligence_summary", "proof_of_address"}


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class Institution:
    def __init__(self, folder):
        self.folder = folder
        self.cases = {c["case_id"]: c for c in load_jsonl(os.path.join(folder, "cases.jsonl"))}
        self.customers = {c["customer_id"]: c for c in load_jsonl(os.path.join(folder, "customers.jsonl"))}
        self.alerts = collections.defaultdict(list)
        for a in load_jsonl(os.path.join(folder, "alerts.jsonl")):
            self.alerts[a["case_id"]].append(a)
        self.tx = collections.defaultdict(list)
        for t in load_jsonl(os.path.join(folder, "transactions.jsonl")):
            self.tx[t["customer_id"]].append(t)
        self.evidence = collections.defaultdict(list)
        for e in load_jsonl(os.path.join(folder, "evidence.jsonl")):
            self.evidence[e["customer_id"]].append(e)
        self.reviews = collections.defaultdict(list)
        for r in load_jsonl(os.path.join(folder, "previous_reviews.jsonl")):
            self.reviews[r["customer_id"]].append(r)
        self.list_entries = {e["entry_id"]: e for e in load_jsonl(os.path.join(folder, "internal_list.jsonl"))}

    def doc_text(self, e):
        with open(os.path.join(self.folder, e["file"]), encoding="utf-8") as fh:
            return fh.read().strip()

    def signature(self, case_id):
        case = self.cases[case_id]
        rules = sorted(a["rule_id"] for a in self.alerts[case_id])
        types = sorted({e["type"] for e in self.evidence[case["customer_id"]]} - BASELINE - {"contact_note"})
        return "+".join(rules) + " | " + ",".join(types)


def tx_line(t, mark=" "):
    amt = "£{:>11,.2f}".format(t["amount_gbp"])
    if t["currency"] != "GBP":
        amt += " ({} {:,.2f})".format(t["currency"], t["amount"])
    parts = [mark, t["transaction_id"], t["booked_at"][:16].replace("T", " "), "{:3s}".format(t["direction"]),
             "{:16s}".format(t["channel"]), amt, "|", str(t["counterparty_name"])]
    if t.get("counterparty_account"):
        parts += ["| acct", t["counterparty_account"]]
    parts += ["|", t["counterparty_country"], "| ref:", str(t["reference"])]
    if t.get("location"):
        parts += ["| loc:", t["location"]]
    return " ".join(parts)


def render(inst, case_id, max_tx=60):
    case = inst.cases[case_id]
    cust = inst.customers[case["customer_id"]]
    out = []
    w = out.append
    w("=" * 110)
    w("CASE {}   window {} to {}   opened {}".format(
        case_id, case["review_window"]["from"], case["review_window"]["to"], case["opened_at"][:10]))
    if case.get("held_payment_id"):
        w("HELD PAYMENT: " + case["held_payment_id"])
    w("=" * 110)

    d = cust["declared"]
    if cust["kind"] == "individual":
        w("CUSTOMER {} {} (individual, born {}) | {} at {} | onboarded {} | risk {}".format(
            cust["customer_id"], cust["name"], cust["date_of_birth"], cust["occupation"], cust["employer"],
            cust["onboarded_on"], cust["risk_rating"]))
    else:
        w("CUSTOMER {} {} (business, co. no. {}, inc. {}) | {} | onboarded {} | risk {}".format(
            cust["customer_id"], cust["name"], cust["company_number"], cust["incorporated_on"],
            cust["business_activity"], cust["onboarded_on"], cust["risk_rating"]))
    w("  address: " + cust["address"])
    w("  declared: income/month £{} | turnover/year £{} | source: {} | purpose: {} | countries: {} | channels: {}".format(
        d["monthly_income_gbp"], d["annual_turnover_gbp"], d["source_of_funds"], d["account_purpose"],
        ", ".join(d["expected_countries"]), ", ".join(d["expected_channels"])))

    flagged = collections.defaultdict(list)
    w("")
    for a in sorted(inst.alerts[case_id], key=lambda a: a["rule_id"]):
        w("ALERT {} [{}] {}".format(a["alert_id"], a["rule_id"], a["rule_name"]))
        w("  " + a["message"])
        w("  measured: " + json.dumps(a["measured"], ensure_ascii=False))
        for tid in a["transaction_ids"]:
            flagged[tid].append(a["rule_id"])
        if a["rule_id"] == "TM22":
            entry = inst.list_entries.get(a["measured"].get("list_entry"))
            w("  LIST ENTRY: " + json.dumps(entry, ensure_ascii=False))

    txs = sorted(inst.tx[case["customer_id"]], key=lambda t: t["booked_at"])
    w("")
    w("TRANSACTIONS: {} on file, {} flagged by the alerts".format(len(txs), len(flagged)))
    shown = [t for t in txs if t["transaction_id"] in flagged or t["transaction_id"] == case.get("held_payment_id")]
    for t in shown[:max_tx]:
        tags = ",".join(sorted(set(flagged.get(t["transaction_id"], [])))) or "HELD"
        w(tx_line(t, "*") + "  <" + tags + ">")
    if len(shown) > max_tx:
        w("  ... {} more flagged transactions not shown".format(len(shown) - max_tx))

    win_from, win_to = case["review_window"]["from"], case["review_window"]["to"]
    shown_ids = {t["transaction_id"] for t in shown}
    others = [t for t in txs if t["transaction_id"] not in shown_ids and win_from <= t["booked_at"][:10] <= win_to
              and t["channel"] != "card" and t["amount_gbp"] >= 250]
    if others:
        w("Other non-card transactions of £250 or more in the review window (not flagged):")
        for t in others[:20]:
            w(tx_line(t))
        if len(others) > 20:
            w("  ... {} more".format(len(others) - 20))
    inflow = collections.defaultdict(float)
    outflow = collections.defaultdict(float)
    for t in txs:
        (inflow if t["direction"] == "in" else outflow)[t["booked_at"][:7]] += t["amount_gbp"]
    months = sorted(set(inflow) | set(outflow))
    w("Monthly totals (in / out): " + "; ".join(
        "{} £{:,.0f} / £{:,.0f}".format(m, inflow[m], outflow[m]) for m in months[-8:]))

    if inst.reviews[case["customer_id"]]:
        w("")
        for r in inst.reviews[case["customer_id"]]:
            w("PREVIOUS REVIEW {} {} [{}] {}: {}".format(r["review_id"], r["reviewed_on"], r["rule_id"], r["verdict"], r["note"]))

    w("")
    docs = sorted(inst.evidence[case["customer_id"]], key=lambda e: (e["type"] in BASELINE, e["dated"]))
    for e in docs:
        w("--- DOCUMENT {} | {} | dated {}".format(e["doc_id"], e["type"], e["dated"]))
        if e["type"] in BASELINE:
            w("    (baseline onboarding document: {})".format(e["title"]))
            continue
        for line in inst.doc_text(e).split("\n"):
            if line.startswith("**Document ID:**") or line.startswith("**Dated:**"):
                continue
            if line.strip():
                w("    " + line)
    w("")
    return "\n".join(out)


def main(argv):
    if len(argv) < 3:
        sys.exit(__doc__)
    inst = Institution(argv[1])
    if argv[2] == "--ids-file":
        with open(argv[3], encoding="utf-8") as fh:
            ids = [line.strip() for line in fh if line.strip()]
    else:
        ids = argv[2:]
    for cid in ids:
        print(render(inst, cid))


if __name__ == "__main__":
    main(sys.argv)
