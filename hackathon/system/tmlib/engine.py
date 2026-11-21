"""Decision framework: collect findings about a case, then turn them into one verdict.

A finding is either an explanation (a document on file that accounts for specific transactions or for a whole
alert) or a suspicion (a sign of crime, which forces an escalation). A case closes only when every alert is
explained. Standard library only.
"""
import re

from . import util
from .util import fmt_date as D
from .util import fmt_money as M

CLOSE, RFI, ESCALATE = "CLOSE", "RFI", "ESCALATE"

# What must remain unexplained for a rule not to have fired, when the alert does not carry its own limit.
DEFAULT_LIMITS = {"TM06": 5000.0, "TM07": 5000.0, "TM08": 1000.0}


class Finding:
    def __init__(self, kind, code, text, evidence, tx_ids=(), alerts=(), rank=50, tag="", own_account=False, brief=None):
        self.kind = kind                    # "explain" or "suspect"
        self.code = code                    # reason code this finding supports
        self.text = text                    # one or two sentences naming the facts
        self.evidence = list(evidence)      # document, list-entry and transaction IDs relied on
        self.tx_ids = set(tx_ids)           # transactions this finding accounts for
        self.alerts = set(alerts)           # alert IDs this finding settles outright
        self.rank = rank                    # higher wins when choosing the main ground
        self.tag = tag
        self.own_account = own_account
        self.brief = brief                  # a one-sentence form, used when this is not the main ground


class Context:
    """One case plus the findings gathered so far."""

    def __init__(self, inst, case):
        self.inst = inst
        self.case = case
        self.findings = []
        self.notes = {}
        self.blocks = []   # things that stop a close even when the alerts are explained: (transaction, what to ask for, fact)

    def add(self, *args, **kwargs):
        f = Finding(*args, **kwargs)
        self.findings.append(f)
        return f

    def explains(self):
        return [f for f in self.findings if f.kind == "explain"]

    def suspicions(self):
        return [f for f in self.findings if f.kind == "suspect"]

    def explained_tx(self):
        ids = set()
        for f in self.explains():
            ids |= f.tx_ids
        return ids

    def is_customer(self, name):
        return util.same_party(name, self.case.name)

    def flagged(self, alert):
        return self.case.alert_transactions(alert)

    def all_flagged_ids(self):
        ids = set()
        for a in self.case.alerts:
            ids |= set(a.get("transaction_ids", []))
        return ids


def party(tx):
    return tx.get("counterparty_name") or "an unnamed party"


def tx_phrase(tx):
    """'the £1,800.00 payment to Elmcote Consulting Ltd on 15 September 2026 (BK-T1)'"""
    when = D(util.parse_date(tx["booked_at"]))
    amount = M(tx["amount_gbp"])
    name = tx.get("counterparty_name")
    channel = tx.get("channel") or ""
    if channel.startswith("cash"):
        what = "cash " + ("deposit" if tx["direction"] == "in" else "withdrawal")
        if channel == "cash_topup":
            what = "cash top-up"
        where = " at " + tx["location"] if tx.get("location") else ""
        return "the {} {} on {}{} ({})".format(amount, what, when, where, tx["transaction_id"])
    if tx["direction"] == "in":
        return "the {} received from {} on {} ({})".format(amount, name or "an unnamed payer", when, tx["transaction_id"])
    return "the {} paid to {} on {} ({})".format(amount, name or "an unnamed payee", when, tx["transaction_id"])


def join_names(names, limit=4):
    names = list(dict.fromkeys(n for n in names if n))
    if len(names) == limit + 1:
        limit += 1
    if len(names) > limit:
        return ", ".join(names[:limit]) + " and {} others".format(len(names) - limit)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


# --------------------------------------------------------------------------------------------------------------
# Is each alert explained?
# --------------------------------------------------------------------------------------------------------------

def alert_status(ctx, alert):
    """(explained, unexplained transactions) for one alert, given the explanations found so far."""
    aid, rule = alert["alert_id"], alert["rule_id"]
    for f in ctx.explains():
        if aid in f.alerts:
            return True, []
    covered = ctx.explained_tx()
    txs = ctx.flagged(alert)
    open_tx = [t for t in txs if t["transaction_id"] not in covered]
    m = alert.get("measured") or {}
    if rule in ("TM10", "TM22"):
        return False, open_tx
    if not txs:
        return False, []
    if rule == "TM01":
        received = m.get("received_gbp") or sum(t["amount_gbp"] for t in txs if t["direction"] == "in")
        allowed = (m.get("limit_multiple") or 0) * (m.get("baseline_gbp") or 0)
        done = sum(t["amount_gbp"] for t in txs if t["direction"] == "in" and t["transaction_id"] in covered)
        open_in = sorted((t for t in open_tx if t["direction"] == "in"), key=lambda t: (-t["amount_gbp"], t["transaction_id"]))
        need, excess, left = [], received - done - allowed, 0.0
        for t in open_in:
            if left >= excess - 0.005 and need:
                break
            need.append(t)
            left += t["amount_gbp"]
        return received - done <= allowed + 0.005, need
    if rule == "TM02" and ctx.inst.tm02_third_party_only:
        # At this institution the rule only counts money from anyone other than the client.
        third = [t for t in txs if t["direction"] == "in" and not ctx.is_customer(t.get("counterparty_name"))]
        open_third = [t for t in third if t["transaction_id"] not in covered]
        left = sum(t["amount_gbp"] for t in open_third)
        return left < max(2000.0, 0.2 * (sum(t["amount_gbp"] for t in third) or 1.0)), open_third
    if rule == "TM02":
        # The rule needs at least £2,000 received and 80% of it paid out. It is explained when what remains
        # unexplained on either side is too small to have fired it.
        open_in = sum(t["amount_gbp"] for t in open_tx if t["direction"] == "in" and not ctx.is_customer(t.get("counterparty_name")))
        all_in = sum(t["amount_gbp"] for t in txs if t["direction"] == "in") or 1.0
        open_out = sum(t["amount_gbp"] for t in open_tx if t["direction"] == "out" and t.get("channel") not in ("card",))
        all_out = sum(t["amount_gbp"] for t in txs if t["direction"] == "out") or 1.0
        ok = open_in < max(2000.0, 0.2 * all_in) and open_out < max(2000.0, 0.2 * all_out) \
            and (open_in + open_out) < 0.2 * (all_in + all_out)
        return ok, [t for t in open_tx if t["amount_gbp"] >= 250]
    if rule == "TM04":
        limit = m.get("limit") or 0
        names = {util.norm_name(t.get("counterparty_name")) for t in open_tx}
        return len(names) <= limit and len(names) <= max(1, len(txs) // 5), open_tx
    if rule in ("TM03", "TM05"):
        return not open_tx, open_tx
    limit = m.get("limit_gbp") or DEFAULT_LIMITS.get(rule, 0.0)
    left = sum(t["amount_gbp"] for t in open_tx)
    return left < limit, open_tx


# --------------------------------------------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------------------------------------------

def _sentences(parts, limit=4):
    out = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in out:
            out.append(p if p.endswith(".") else p + ".")
    return " ".join(out[:limit])


def _evidence(case, *groups, cap=12):
    """Cited IDs in order, documents and list entries before transactions, without repeats."""
    docs, txs = [], []
    for group in groups:
        for e in group:
            if not e:
                continue
            target = txs if e in case.tx_by_id else docs
            if e not in target:
                target.append(e)
    return (docs + txs[:max(2, cap - len(docs))])[:max(cap, len(docs))]


def _closing_sentence(ctx, explains):
    """Say why every alert is now accounted for."""
    case = ctx.case
    covered = ctx.explained_tx()
    rules = ", ".join(case.rules)
    tm01 = case.alert("TM01")
    if tm01:
        m = tm01.get("measured") or {}
        done = sum(t["amount_gbp"] for t in ctx.flagged(tm01) if t["direction"] == "in" and t["transaction_id"] in covered)
        rest = (m.get("received_gbp") or 0) - done
        allowed = (m.get("limit_multiple") or 0) * (m.get("baseline_gbp") or 0)
        if done > 0 and rest >= 0:
            return "With that explained, the other receipts in the 30 days come to {}, inside the limit of {} ({:g} times the declared {}), " \
                   "so every alert in the case ({}) is accounted for".format(M(rest), M(allowed), m.get("limit_multiple", 0), M(m.get("baseline_gbp", 0)), rules)
    if len(case.alerts) > 1:
        return "The same documents account for every alert in the case ({}), and nothing else in the activity is unexplained".format(rules)
    return "Nothing else in the alerted activity is unexplained, so the {} alert can be closed".format(rules)


def decide(ctx, rfi_builder):
    case = ctx.case
    suspicions = sorted(ctx.suspicions(), key=lambda f: -f.rank)
    if suspicions:
        main = suspicions[0]
        others = [f for f in suspicions[1:] if f.text != main.text]
        reason = _sentences([main.text] + [f.brief or f.text for f in others if f.brief or len(others) == 1][:2])
        evidence = _evidence(case, main.evidence, *[f.evidence for f in others])
        return {"verdict": ESCALATE, "reason_code": main.code, "reason": reason, "evidence": evidence}

    statuses = [(a,) + alert_status(ctx, a) for a in case.alerts]
    unexplained = [(a, open_tx) for a, ok, open_tx in statuses if not ok]
    explains = sorted(ctx.explains(), key=lambda f: -f.rank)

    if ctx.blocks and not unexplained:
        return rfi_builder(ctx, [], explains)

    if not unexplained and explains:
        # Policy: own-account transfers that take activity to 5 times the declared figure need an updated profile.
        tm01 = case.alert("TM01")
        if tm01:
            m = tm01.get("measured") or {}
            flagged_in = [t for t in ctx.flagged(tm01) if t["direction"] == "in"]
            own_ids = set()
            for f in explains:
                if f.own_account:
                    own_ids |= f.tx_ids
            own_value = sum(t["amount_gbp"] for t in flagged_in if t["transaction_id"] in own_ids)
            other_value = sum(t["amount_gbp"] for t in flagged_in if t["transaction_id"] not in own_ids)
            allowed = (m.get("limit_multiple") or 0) * (m.get("baseline_gbp") or 0)
            relies_on_own = own_value > 0 and (m.get("received_gbp", 0) - own_value) <= allowed + 0.005 \
                and other_value <= allowed + 0.005
            if relies_on_own and (m.get("multiple") or 0) >= ctx.inst.own_account_multiple:
                own = [f for f in explains if f.own_account]
                unit = "turnover" if case.is_business else "income"
                if ctx.inst.name == "payments_fx":
                    unit = "payment volume"
                fresh = ctx.notes.get("updated_profile")
                if fresh and (m.get("received_gbp") or 0) / fresh["monthly"] < ctx.inst.own_account_multiple:
                    # The updated figure the policy would ask for is already on file, and the activity sits inside it.
                    reason = _sentences([
                        own[0].text,
                        "Against the profile declared at onboarding that is {:g} times the monthly {}, but {} (dated {}) records an updated figure "
                        "({}: {}), and receipts of {} are {:.1f} times that, under the {:g} times line in the policy, so the activity fits the "
                        "customer's current declared profile".format(
                            m.get("multiple"), unit, fresh["doc"].id, D(fresh["doc"].date), fresh["label"], fresh["value"],
                            M(m.get("received_gbp", 0)), (m.get("received_gbp") or 0) / fresh["monthly"], ctx.inst.own_account_multiple)])
                    return {"verdict": CLOSE, "reason_code": "WITHIN_PROFILE", "reason": reason,
                            "evidence": _evidence(case, [fresh["doc"].id], *[f.evidence for f in own])}
                reason = _sentences([
                    own[0].text,
                    "That explains the money, but receipts of {} in the 30 days are {:g} times the declared monthly {} of {}, "
                    "at or above the {:g} times line in the policy".format(
                        M(m.get("received_gbp", 0)), m.get("multiple"), unit, M(m.get("baseline_gbp", 0)), ctx.inst.own_account_multiple),
                    "Ask {} for an updated expected {} figure for the account".format(
                        "the client" if case.is_business else "the customer",
                        "annual payment volume" if ctx.inst.name == "payments_fx" else
                        ("annual turnover" if case.is_business else "monthly income and expected deposits"))])
                return {"verdict": RFI, "reason_code": "RFI_UPDATED_PROFILE", "reason": reason,
                        "evidence": _evidence(case, *[f.evidence for f in own])}
        main = explains[0]
        others = [f for f in explains[1:] if f.text != main.text and not (f.tx_ids and f.tx_ids <= main.tx_ids and not f.alerts)]
        closing = _closing_sentence(ctx, explains)
        parts = [main.text] + [f.text for f in others][:2]
        count = sum(len(re.findall(r"[.!?](?:\s|$)", p + ".")) for p in parts)
        reason = _sentences(parts + ([closing] if count < 4 else []), limit=5)
        evidence = _evidence(case, main.evidence, *[f.evidence for f in others])
        return {"verdict": CLOSE, "reason_code": main.code, "reason": reason, "evidence": evidence}

    if not unexplained and not explains:
        unexplained = [(a, ctx.flagged(a)) for a in case.alerts]
    return rfi_builder(ctx, unexplained, explains)
