"""Run the alert-closing engine on one case and record every step it took, for the screening UI.

This imports the submitted system from hackathon/system and calls the same checks in the same order, so
what the UI shows is what the system did. It is a viewer; it is not part of the submission.
"""
import os
import sys

sys.dont_write_bytecode = True
SYSTEM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hackathon", "system")
sys.path.insert(0, SYSTEM_DIR)

from tmlib import data, detect, engine, util  # noqa: E402

BASELINE = ("due_diligence_summary", "proof_of_address")

CHECK_TITLES = {
    "own_account": "Own-account statement",
    "event_documents": "Letter or statement for a one-off event",
    "invoices": "Invoice against payment",
    "invoice_tables": "Invoice schedule, contract stages or payment file",
    "group_company": "Group structure",
    "cash_records": "Business cash records",
    "cash_sale": "Cash sale receipt",
    "cash_redeposit": "Cash paid back in",
    "refunds": "Refund of an earlier payment",
    "group_booking": "Group booking",
    "cancellation_refund": "Cancellation and return of funds",
    "early_redemption_reason": "Reason for early redemption",
    "own_funding": "Client's own funding",
    "returning_customer": "Re-verified returning customer",
    "list_match": "Internal list match",
    "held_payment_needs_invoice": "Invoice needed for a held payment",
    "updated_profile": "Updated profile on file",
    "account_control": "Someone else controlling the account",
    "fraud_reports": "Fraud reports from other banks",
    "chat_admissions": "What the customer said",
    "shared_device": "Shared device and forwarding",
    "staff_notes": "Staff concern on file",
    "dissolved_payer": "Payer dissolved on the register",
    "linked_party_pass_through": "Pass-through to linked companies",
    "cash_moved_on": "Cash paid in and sent on",
    "corridor_fan_out": "Fan-out to a higher-risk country",
    "third_party_payout": "Outsider's money paid to another outsider",
    "third_party_corridor_funding": "Third-party funding from a higher-risk country",
}


def _tx(case, t, flagged_by, explained_by):
    return {
        "id": t["transaction_id"],
        "at": t["booked_at"][:16].replace("T", " "),
        "direction": t["direction"],
        "channel": t.get("channel"),
        "amount_gbp": t["amount_gbp"],
        "currency": t.get("currency"),
        "amount": t.get("amount"),
        "counterparty": t.get("counterparty_name"),
        "account": t.get("counterparty_account"),
        "country": t.get("counterparty_country"),
        "reference": t.get("reference"),
        "location": t.get("location"),
        "flagged_by": sorted(flagged_by.get(t["transaction_id"], [])),
        "explained_by": explained_by.get(t["transaction_id"]),
        "held": bool(case.held_payment and case.held_payment["transaction_id"] == t["transaction_id"]),
    }


def _alert_test(ctx, alert, source):
    """The arithmetic behind 'is this alert explained?', in words."""
    explained, open_tx = engine.alert_status(ctx, alert)
    rule, m = alert["rule_id"], alert.get("measured") or {}
    covered = ctx.explained_tx()
    flagged = ctx.flagged(alert)
    settled = [f for f in ctx.explains() if alert["alert_id"] in f.alerts]
    M = util.fmt_money
    if settled:
        note = "Settled directly by the check \"{}\".".format(source.get(id(settled[0]), settled[0].code.replace("_", " ").lower()))
    elif rule == "TM01":
        done = sum(t["amount_gbp"] for t in flagged if t["direction"] == "in" and t["transaction_id"] in covered)
        received = m.get("received_gbp") or 0
        allowed = (m.get("limit_multiple") or 0) * (m.get("baseline_gbp") or 0)
        note = "Received {} − explained {} = {} left. The rule allows {} ({:g} × {}).".format(
            M(received), M(done), M(received - done), M(allowed), m.get("limit_multiple") or 0, M(m.get("baseline_gbp") or 0))
    elif rule == "TM02" and ctx.inst.tm02_third_party_only:
        third = [t for t in flagged if t["direction"] == "in" and not ctx.is_customer(t.get("counterparty_name"))]
        left = sum(t["amount_gbp"] for t in third if t["transaction_id"] not in covered)
        note = "Here the rule counts only money from third parties: {} received, {} of it left unexplained.".format(
            M(sum(t["amount_gbp"] for t in third)), M(left))
    elif rule in ("TM10", "TM22"):
        note = "This alert needs a document that settles it directly; none was found." if not explained else ""
        if ctx.suspicions():
            note = "Not settled: the checks found a sign of crime on this activity instead."
    else:
        total = sum(t["amount_gbp"] for t in flagged)
        left = sum(t["amount_gbp"] for t in flagged if t["transaction_id"] not in covered)
        note = "{} flagged transaction{} worth {}; {} left unexplained.".format(
            len(flagged), "" if len(flagged) == 1 else "s", M(total), M(left))
    return {"explained": bool(explained), "open": [t["transaction_id"] for t in open_tx], "note": note}


def next_action(inst, case, decision):
    """What the analyst does with the verdict: a message, a referral or a closing note, ready to copy."""
    import datetime
    import re
    who = "client" if case.is_business else "customer"
    verdict, reason, code = decision["verdict"], decision["reason"], decision["reason_code"]
    held = case.held_payment
    evidence = ", ".join(decision["evidence"])
    opened = util.parse_date(case.row.get("opened_at") or "")
    if verdict == "RFI":
        m = re.search(r"[Aa]sk the (?:customer|client) for (.+?)(?:\.\s+[A-Z]|\.$)", reason)
        ask = (m.group(1) if m else "documents showing what the recent payments on the account were for").strip()
        due = util.fmt_date(opened + datetime.timedelta(days=14)) if opened else "within 14 days"
        return {
            "title": "Request to send to the {}".format(who),
            "note": "Asks for one specific item and says nothing about alerts or monitoring." + (
                " The held payment {} stays held until the reply is reviewed.".format(held["transaction_id"]) if held else ""),
            "text": "Subject: Information needed about recent activity on your account\n\nDear {},\n\n"
                    "As part of our routine account checks we need some information about recent activity on your account.\n\n"
                    "Please send us {}.\n\nYou can reply to this message or upload the document in the app. Please reply by {}."
                    "\n\nThank you,\nAccount Review Team".format(case.name, ask[0].lower() + ask[1:], due),
        }
    if verdict == "ESCALATE":
        return {
            "title": "Referral to the MLRO",
            "warning": "Do not contact the {} about this case. Once there is suspicion, asking questions risks tipping them off, which is an offence.".format(who),
            "note": "Internal only." + (" The held payment {} stays held.".format(held["transaction_id"]) if held else ""),
            "text": "INTERNAL - DO NOT DISCLOSE TO THE {}\n\nReferral to the MLRO\nCase: {} ({})\n{}: {} ({})\nGround: {}\n"
                    "Alerts: {}\n{}\nSummary: {}\n\nEvidence relied on: {}\n\nRequested: MLRO decision on whether to file a suspicious activity report.".format(
                        who.upper(), case.id, inst.name, who.capitalize(), case.name, case.customer.get("customer_id"), code,
                        ", ".join("{} {}".format(a["rule_id"], a.get("rule_name") or "") for a in case.alerts),
                        "Held payment: {} ({}), remains held\n".format(held["transaction_id"], util.fmt_money(held["amount_gbp"])) if held else "",
                        reason, evidence),
        }
    return {
        "title": "Closing note for the case record",
        "note": "Release the held payment {} ({}).".format(held["transaction_id"], util.fmt_money(held["amount_gbp"])) if held else
                "No action needed from the {}.".format(who),
        "text": "Case {} closed.\nGround: {}\nAlerts covered: {}\n{}\nReason: {}\n\nEvidence relied on: {}".format(
            case.id, code, ", ".join(a["rule_id"] for a in case.alerts),
            "Held payment {} released.\n".format(held["transaction_id"]) if held else "", reason, evidence),
    }


def build(inst, case):
    """Everything the UI needs to replay the screening of one case."""
    ctx = engine.Context(inst, case)
    checks = []
    source = {}   # which check produced each finding
    for phase, check in detect.CHECKS:
        before_findings, before_blocks = len(ctx.findings), len(ctx.blocks)
        grown = {id(f): len(f.tx_ids) for f in ctx.findings}
        check(ctx)
        new = ctx.findings[before_findings:]
        extended = [f for f in ctx.findings[:before_findings] if len(f.tx_ids) > grown[id(f)]]
        blocks = ctx.blocks[before_blocks:]
        for f in new:
            source[id(f)] = CHECK_TITLES.get(check.__name__, check.__name__.replace("_", " ").capitalize())
        outcome = "nothing"
        if any(f.kind == "suspect" for f in new):
            outcome = "suspicion"
        elif new or extended:
            outcome = "explanation"
        elif blocks:
            outcome = "block"
        checks.append({
            "name": check.__name__,
            "title": CHECK_TITLES.get(check.__name__, check.__name__.replace("_", " ").capitalize()),
            "question": (check.__doc__ or "").strip().split("\n")[0],
            "phase": phase,
            "outcome": outcome,
            "findings": [{"kind": f.kind, "code": f.code, "text": f.text, "evidence": f.evidence, "tx_ids": sorted(f.tx_ids)}
                         for f in new] +
                        [{"kind": "explain", "code": f.code, "text": "Also covers the client's own funding that paid for it.",
                          "evidence": f.evidence[-1:], "tx_ids": sorted(f.tx_ids)} for f in extended],
            "blocks": [{"fact": b["fact"], "ask": b["ask"], "tx": b["tx"]["transaction_id"]} for b in blocks],
        })

    decision = engine.decide(ctx, detect.build_rfi)
    alerts = []
    for a in case.alerts:
        alerts.append({
            "alert_id": a["alert_id"], "rule_id": a["rule_id"], "rule_name": a.get("rule_name"), "message": a.get("message"),
            "measured": a.get("measured"), "transaction_ids": a.get("transaction_ids", []), "test": _alert_test(ctx, a, source),
        })

    flagged_by = {}
    for a in case.alerts:
        for tid in a.get("transaction_ids", []):
            flagged_by.setdefault(tid, []).append(a["rule_id"])
    explained_by = {}
    for f in ctx.explains():
        for tid in f.tx_ids:
            explained_by.setdefault(tid, source.get(id(f)) or f.code.replace("_", " ").capitalize())
    cited = set(decision["evidence"])
    for f in ctx.findings:
        cited |= set(f.evidence) | f.tx_ids
    keep = set(flagged_by) | {t for t in cited if t in case.tx_by_id}
    if case.held_payment:
        keep.add(case.held_payment["transaction_id"])
    context = [t for t in case.transactions if t["transaction_id"] not in keep and t.get("channel") != "card"
               and t["amount_gbp"] >= 250 and case.window_from and case.window_to
               and case.window_from <= util.parse_date(t["booked_at"]) <= case.window_to][:30]
    keep |= {t["transaction_id"] for t in context}
    transactions = [_tx(case, t, flagged_by, explained_by) for t in case.transactions if t["transaction_id"] in keep]

    documents = [{
        "id": d.id, "type": d.type, "title": d.title, "dated": util.fmt_date(d.date) if d.date else None,
        "text": d.raw, "baseline": d.type in BASELINE, "relied_on": d.id in cited,
    } for d in sorted(case.documents, key=lambda d: (d.type in BASELINE, d.type == "contact_note", d.id))]

    entries = []
    for a in case.alerts:
        entry = inst.list_entries.get((a.get("measured") or {}).get("list_entry"))
        if entry:
            entries.append(entry)

    cust = case.customer
    policy = [{"label": "Higher-risk countries", "value": ", ".join(sorted(inst.higher_risk_countries))}]
    policy.append({"label": "Own-account transfers need an updated profile at",
                   "value": "{:g} times the declared figure".format(inst.own_account_multiple)})
    policy.append({"label": "Rapid-movement rule counts",
                   "value": "only money from third parties" if inst.tm02_third_party_only else "all money received"})
    policy.append({"label": "Invoice needed before releasing a payment of",
                   "value": util.fmt_money(inst.invoice_threshold) + " or more to anyone but the client" if inst.invoice_threshold else "not applicable here"})

    return {
        "case": {"id": case.id, "institution": inst.name, "window_from": util.fmt_date(case.window_from) if case.window_from else None,
                 "window_to": util.fmt_date(case.window_to) if case.window_to else None,
                 "opened_at": (case.row.get("opened_at") or "")[:10], "held_payment_id": case.row.get("held_payment_id"),
                 "transactions_on_file": len(case.transactions)},
        "customer": {"id": cust.get("customer_id"), "name": cust.get("name"), "kind": cust.get("kind"),
                     "occupation": cust.get("occupation"), "employer": cust.get("employer"),
                     "business_activity": cust.get("business_activity"), "date_of_birth": cust.get("date_of_birth"),
                     "company_number": cust.get("company_number"), "onboarded_on": cust.get("onboarded_on"),
                     "risk_rating": cust.get("risk_rating"), "address": cust.get("address"), "declared": cust.get("declared") or {}},
        "policy": policy,
        "alerts": alerts,
        "transactions": transactions,
        "documents": documents,
        "reviews": case.reviews,
        "list_entries": entries,
        "checks": checks,
        "decision": decision,
        "next": next_action(inst, case, decision),
    }


class Desk:
    """Loads institutions on demand and keeps the queue (verdict per case) in memory."""

    def __init__(self, root):
        self.root = root
        self.names = sorted(n for n in os.listdir(root) if os.path.exists(os.path.join(root, n, "cases.jsonl")))
        self._inst, self._cases, self._queue, self._rules = {}, {}, {}, {}

    def _load(self, name):
        if name not in self._inst:
            inst = data.Institution(os.path.join(self.root, name))
            cases = {c.id: c for c in inst.cases()}
            queue = []
            for case in cases.values():
                ctx = engine.Context(inst, case)
                detect.run_all(ctx)
                d = engine.decide(ctx, detect.build_rfi)
                queue.append({"id": case.id, "customer": case.name, "kind": case.customer.get("kind"), "rules": case.rules,
                              "verdict": d["verdict"], "reason_code": d["reason_code"],
                              "held": bool(case.held_payment), "opened": (case.row.get("opened_at") or "")[:10]})
            names = {}
            for case in cases.values():
                for a in case.alerts:
                    names.setdefault(a["rule_id"], a.get("rule_name") or a["rule_id"])
            self._inst[name], self._cases[name], self._queue[name], self._rules[name] = inst, cases, queue, names
        return self._inst[name]

    def queue(self, name):
        self._load(name)
        return self._queue[name]

    def rules(self, name):
        """Rule ID to rule name, for the rules this institution runs."""
        self._load(name)
        return self._rules[name]

    def policy(self, name):
        """The institution's SOP text."""
        return self._load(name).policy

    def trace(self, name, case_id):
        inst = self._load(name)
        case = self._cases[name].get(case_id)
        return build(inst, case) if case else None
