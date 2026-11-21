"""Detectors: each reads the case and records explanations or suspicions on the context.

Nothing here refers to a particular case, customer, amount or document. Every check compares what the
documents on file say with what the transactions show. Standard library only.
"""
import re

from . import util
from .engine import D, M, alert_status, join_names, party, tx_phrase

ACCOUNT_RE = re.compile(r"\b\d{2}-\d{2}-\d{2}\s+\d{8}\b|(?:account|IBAN)\s+ending\s+\d{4}", re.I)
SUPPORT_SKIP = ("due_diligence_summary", "proof_of_address", "contact_note")
IGNORE_CHAT_WORDING = False   # set by the robustness test only


def tx_date(tx):
    return util.parse_date(tx["booked_at"])


def has_amount(doc, amount):
    return any(util.same_amount(amount, a) for a in doc.amounts())


def accounts_in(text):
    return ACCOUNT_RE.findall(text or "")


def doc_has_account(doc, account):
    return bool(account) and any(util.same_account(account, a) for a in accounts_in(doc.raw))


def title_party(doc):
    """The party a document is from, taken from titles such as 'Gift letter from Jules Holldale'."""
    m = re.search(r".*\b(?:from|with|for|by)\s+(.+)$", doc.title)
    return m.group(1).strip().split(":")[0].strip() if m else ""


def doc_ref(doc):
    kind = doc.type.replace("_", " ")
    return "{} ({}, dated {})".format(doc.id, kind, D(doc.date)) if doc.date else "{} ({})".format(doc.id, kind)


def in_window(case, tx, slack=0):
    d = tx_date(tx)
    if not case.window_from or not case.window_to:
        return True
    return (case.window_from - _days(slack)) <= d <= (case.window_to + _days(slack))


def _days(n):
    import datetime
    return datetime.timedelta(days=n)


def flagged_ids(case):
    ids = set()
    for a in case.alerts:
        ids |= set(a.get("transaction_ids", []))
    return ids


def relevant(case, tx):
    """A transaction the alerts are about: flagged, the held payment, or inside the review window."""
    return tx["transaction_id"] in flagged_ids(case) or tx is case.held_payment or in_window(case, tx, 3)


# --------------------------------------------------------------------------------------------------------------
# Explanations
# --------------------------------------------------------------------------------------------------------------

def own_account(ctx):
    """A statement on file shows the customer holds the account on the other side of the transfer."""
    case = ctx.case
    for doc in case.docs("bank_statement"):
        holders = doc.field("Account holder", "Account holders") or ""
        found = accounts_in(doc.raw)
        account = doc.field("Account") or ""
        if not accounts_in(account) and found:
            account = found[0]
        if not account:
            continue
        if not util.mentions(holders, case.name):
            ctx.notes.setdefault("statement_not_customer", []).append(doc.id)
            continue
        txs = [t for t in case.transactions if relevant(case, t)
               and any(util.same_account(t.get("counterparty_account"), a) for a in [account] + found)]
        txs = [t for t in txs if has_amount(doc, t["amount_gbp"])] or []
        if not txs:
            continue
        bank = doc.field("Bank") or title_party(doc)
        joint = " jointly" if re.search(r"\band\b|&", holders) and util.norm_name(holders) != util.norm_name(case.name) else ""
        acct_name = doc.field("Account name")
        text = "{} from {} names {} as{} holding account {}{}, and it records {}".format(
            doc_ref(doc), bank, holders, joint, account,
            " (account name {})".format(acct_name) if acct_name else "",
            join_names([tx_phrase(t) for t in txs], 3))
        support, extra = [], ""
        for other in case.documents:
            if other.type in ("customer_letter", "customer_email", "employment_contract") and (
                    doc_has_account(other, account) or any(has_amount(other, t["amount_gbp"]) for t in txs)
                    or other.type == "employment_contract"):
                support.append(other)
        if support:
            extra = ". " + _support_sentence(ctx, support, txs)
        ctx.add("explain", "OWN_ACCOUNT", text + extra, [doc.id] + [s.id for s in support] + [t["transaction_id"] for t in txs],
                tx_ids=[t["transaction_id"] for t in txs], rank=60, tag="own_account", own_account=True)


def _support_sentence(ctx, docs, txs):
    bits = []
    countries = {t.get("counterparty_country") for t in txs} - {"United Kingdom", None}
    for d in docs:
        if d.type == "customer_letter":
            bits.append("the customer's letter {} tells the firm to pay withdrawals to that account in their own name{}".format(
                d.id, " after relocating abroad" if re.search(r"relocat|moved", d.body, re.I) else ""))
        elif d.type == "customer_email":
            if re.search(r"error|accident|mistake|by mistake", d.body, re.I):
                bits.append("the customer's email {} of {} says the transfer was sent in error and asks for it to be returned to the same account".format(d.id, D(d.date)))
            else:
                bits.append("the customer's email {} of {} gives the instruction".format(d.id, D(d.date)))
        elif d.type == "employment_contract":
            places = re.findall(r"\b([A-Z][a-z]+)(?:[- ]based)? (?:location|office|site|base|branch|facility|operations?|field|project)\b|"
                                r"\b(?:location|office|site|base|branch|facility|based|located|stationed|posted) (?:in|at) ([A-Z][a-z]+)\b", d.body)
            places = [a or b for a, b in places if (a or b) not in ("The", "This", "Its", "Their", "Company", "Our", "Local", "Each")]
            place = places[0] if places else None
            employer = re.search(r"\b([A-Z][\w&'-]+(?: [A-Z][\w&'-]+)* (?:Ltd|LLP|plc|Limited|LLC|GmbH))\b", d.body)
            bits.append("the employment contract {}{} has the customer working {}, paid into a local account, which explains the {} account".format(
                d.id, " with " + employer.group(1) if employer else "", "in " + place if place else "abroad",
                "/".join(sorted(countries)) or "overseas"))
    s = "; ".join(bits)
    return s[:1].upper() + s[1:]


EVENT_DOCS = {
    # type: (reason code, what the document evidences, rank, direction of the money)
    "completion_statement": ("DOCUMENTED_EVENT", "the sale of a property", 62, "in"),
    "insurer_letter": ("DOCUMENTED_EVENT", "an insurance claim settlement", 62, "in"),
    "solicitor_letter": ("DOCUMENTED_EVENT", "an inheritance", 62, "in"),
    "settlement_letter": ("DOCUMENTED_EVENT", "a settlement agreement payment", 62, "in"),
    "deposit_return_letter": ("DOCUMENTED_EVENT", "the return of a tenancy deposit", 62, "in"),
    "holding_deposit_receipt": ("DOCUMENTED_EVENT", "a holding deposit paid for a new tenancy", 61, "out"),
    "gift_letter": ("THIRD_PARTY_DOCUMENTED", "a gift", 61, "in"),
    "loan_agreement": ("DOCUMENTED_EVENT", "a personal loan", 55, "in"),
    "remittance_advice": ("THIRD_PARTY_DOCUMENTED", "an invoice-finance advance", 61, "in"),
}


def payout_amounts(doc):
    """The figures a document says were actually paid: its labelled payout field when it has one, otherwise every figure."""
    paid = [util.parse_money(v) for k, v in doc.fields.items()
            if re.match(r"(paid to|advance|refund|net |amount paid|payout|settlement|principal|loan amount|total amount of credit)", k, re.I)]
    paid = [a for a in paid if a is not None]
    return paid or doc.amounts()


def near(doc, tx, before=10, after=60):
    """True when the transaction falls within a sensible window of the document's date."""
    return not doc.date or -before <= (tx_date(tx) - doc.date).days <= after


def event_documents(ctx):
    """A letter or statement on file evidences a one-off event and matches the other party and the amount."""
    case = ctx.case
    for doc in case.documents:
        if doc.type not in EVENT_DOCS:
            continue
        code, what, rank, direction = EVENT_DOCS[doc.type]
        named = doc.field("Client", "Customer", "Beneficiary", "Payee", "Borrower")
        if named and not util.mentions(named, case.name):
            ctx.notes.setdefault("document_names_someone_else", []).append(doc.id)
            continue
        source = doc.field("Lender") or title_party(doc)
        paid = payout_amounts(doc)
        txs = [t for t in case.transactions if t["direction"] == direction and relevant(case, t)
               and any(util.same_amount(t["amount_gbp"], a) for a in paid) and near(doc, t)
               and (doc.mentions(t.get("counterparty_name")) or util.party_like(source, t.get("counterparty_name")))]
        if not txs:
            if any(has_amount(doc, t["amount_gbp"]) for t in case.transactions if t["direction"] == direction and relevant(case, t)):
                ctx.notes.setdefault("document_does_not_match", []).append(doc.id)
            continue
        details = _event_details(doc, case)
        text = "{} from {} evidences {}{}; its figure of {} and its {} match {}".format(
            doc_ref(doc), source or party(txs[0]), what, details, M(txs[0]["amount_gbp"]),
            "sender" if direction == "in" else "payee", join_names([tx_phrase(t) for t in txs], 3))
        ev = [doc.id]
        if doc.type == "remittance_advice":
            for fa in case.docs("factoring_agreement"):
                if fa.mentions(source):
                    rate = re.search(r"(\d{1,3})%", fa.body)
                    text += ". {} sets up the facility: {} advances {} of the client's approved invoices into this account".format(
                        doc_ref(fa), source, rate.group(0) if rate else "a share")
                    ev.append(fa.id)
        ctx.add("explain", code, text, ev + [t["transaction_id"] for t in txs],
                tx_ids=[t["transaction_id"] for t in txs], rank=rank, tag=doc.type)


def _event_details(doc, case):
    body = doc.body
    bits = []
    m = re.search(r"estate of the late ([A-Z][\w'-]+(?: [A-Z][\w'-]+)+)", body)
    if m:
        bits.append("from the estate of the late " + m.group(1))
    m = re.search(r"\b(CLAIM \d+)", body)
    if m:
        bits.append("claim reference " + m.group(1))
    m = re.search(r"under your ([a-z ]+ policy)", body)
    if m:
        bits.append("under the customer's " + m.group(1))
    prop = doc.field("Property sold")
    if prop:
        bits.append("of " + prop)
    m = re.search(r"to my (\w+(?: \w+)?),", body)
    if m:
        bits.append("to the customer as the donor's " + m.group(1))
    m = re.search(r"compensation for ([a-z ]+)\.", body)
    if m:
        bits.append("as compensation for " + m.group(1).replace("your ", "the customer's "))
    financed = doc.field("Invoices financed")
    if financed:
        bits.append("against {}".format(re.sub(r"^(\d+), total", r"\1 invoices totalling", financed)))
    m = re.search(r"(?:Loan )?Purpose:\**\s*(.+)", doc.raw)
    if m:
        bits.append("for the " + m.group(1).strip().lower())
    return (" (" + ", ".join(bits) + ")") if bits else ""


def cancellation_refund(ctx):
    """Money in was returned to the account it came from, and a cancellation confirmation says so."""
    case = ctx.case
    for doc in case.docs("cancellation_confirmation"):
        for t_in in case.transactions:
            if t_in["direction"] != "in" or not relevant(case, t_in) or not has_amount(doc, t_in["amount_gbp"]):
                continue
            outs = [t for t in case.transactions if t["direction"] == "out" and util.same_amount(t["amount_gbp"], t_in["amount_gbp"])
                    and t["booked_at"] >= t_in["booked_at"]
                    and util.same_account(t.get("counterparty_account"), t_in.get("counterparty_account"))]
            if not outs or not doc_has_account(doc, t_in.get("counterparty_account")):
                continue
            t_out = outs[0]
            product = re.search(r"cancelled your ([\w ]+?) within", doc.body)
            text = "{} confirms the customer cancelled {} within the cooling-off period and that the subscription of {} was returned " \
                   "to the account it came from, {}; that matches {} and {}".format(
                       doc_ref(doc), "their " + product.group(1) if product else "the product", M(t_in["amount_gbp"]),
                       t_in.get("counterparty_account"), tx_phrase(t_in), tx_phrase(t_out))
            ids = [t_in["transaction_id"], t_out["transaction_id"]]
            ctx.add("explain", "REFUND_OF_EARLIER_PAYMENT", text, [doc.id] + ids, tx_ids=ids, rank=63, tag="cancellation")
            return


def early_redemption_reason(ctx):
    """A documented change in circumstances explains why fund units were sold early."""
    case = ctx.case
    alert = case.alert("TM10")
    if not alert:
        return
    letters = case.docs("redundancy_letter")
    emails = case.docs("customer_email", "customer_letter")
    if not letters:
        return
    letter = letters[0]
    employer = title_party(letter)
    flagged = ctx.flagged(alert)
    sells = [t for t in flagged if t["direction"] == "in"]
    sale_date = tx_date(sells[-1]) if sells else None
    if case.customer.get("employer") and employer and not util.same_party(employer, case.customer.get("employer")):
        ctx.notes["redundancy_other_employer"] = letter.id
        return
    if sale_date and letter.date and letter.date > sale_date:
        return
    email = next((e for e in emails if re.search(r"redundan|lost my job|laid off", e.body, re.I)
                  and (not sale_date or not e.date or e.date <= sale_date)), None)
    # The proceeds must go back to the customer, not to someone else.
    later_out = [t for t in case.transactions if t["direction"] == "out" and t.get("channel") not in ("fund_subscription",)
                 and sale_date and tx_date(t) >= sale_date]
    if any(not ctx.is_customer(t.get("counterparty_name")) for t in later_out):
        return
    m = alert.get("measured") or {}
    text = "{} from {}, the customer's declared employer, confirms their role was made redundant".format(doc_ref(letter), employer)
    if email:
        text += ", and in {} of {} the customer asks to sell the fund to cover living costs".format(email.id, D(email.date))
    text += ". That explains selling {} after {} days for {}{}".format(
        m.get("fund", "the fund"), m.get("held_days", "fewer than 90"), M(m.get("redeemed_gbp", 0)),
        ", with the proceeds paid to an account in the customer's own name ({})".format(later_out[0]["transaction_id"]) if later_out else "")
    ev = [letter.id] + ([email.id] if email else []) + [t["transaction_id"] for t in flagged[-2:]] + [t["transaction_id"] for t in later_out[:1]]
    ctx.add("explain", "DOCUMENTED_EVENT", text, ev, alerts=[alert["alert_id"]], rank=62, tag="redundancy")


def returning_customer(ctx):
    """The customer was re-verified before a dormant account was used again, and the money is explained."""
    case = ctx.case
    alert = case.alert("TM07")
    if not alert:
        return
    records = case.docs("reverification_record")
    flagged = ctx.flagged(alert)
    if not records or not flagged:
        return
    rec = records[0]
    first = min(tx_date(t) for t in flagged)
    if rec.date and rec.date > first:
        ctx.notes["reverified_after_activity"] = rec.id
        return
    covered = ctx.explained_tx()
    m = alert.get("measured") or {}
    left = sum(t["amount_gbp"] for t in flagged if t["transaction_id"] not in covered)
    if left >= (m.get("limit_gbp") or 5000.0) or not any(t["transaction_id"] in covered for t in flagged):
        return
    why = re.search(r"The customer said ([^.]+)\.", rec.body)
    basis = [f for f in ctx.explains() if f.tx_ids & {t["transaction_id"] for t in flagged}]
    text = "{} shows the customer's identity was re-verified on {}, before the account was used again after {} dormant days{}".format(
        doc_ref(rec), D(rec.date), m.get("dormant_days", "over 180"),
        "; the customer said " + why.group(1) if why else "")
    if basis:
        text += ". " + basis[0].text
    ev = [rec.id] + [e for f in basis for e in f.evidence]
    ctx.add("explain", "RETURNING_CUSTOMER", text, ev, alerts=[alert["alert_id"]],
            tx_ids=[t["transaction_id"] for t in flagged if t["transaction_id"] in covered], rank=70, tag="returning",
            own_account=any(f.own_account for f in basis))


# --------------------------------------------------------------------------------------------------------------
# Internal list matches
# --------------------------------------------------------------------------------------------------------------

def list_match(ctx):
    """Compare the matched party with the list entry using what the documents say about them."""
    case = ctx.case
    for alert in case.alerts:
        if alert["rule_id"] != "TM22":
            continue
        m = alert.get("measured") or {}
        entry = ctx.inst.list_entries.get(m.get("list_entry")) or {}
        entry_id = entry.get("entry_id") or m.get("list_entry")
        txs = ctx.flagged(alert)
        tx_ids = [t["transaction_id"] for t in txs]
        listed_name = entry.get("name") or ""
        listed_dob = util.parse_date(entry.get("date_of_birth"))
        verdict = None

        # 1. A letter written by the matched person that states their own date of birth.
        for doc in case.documents:
            if not listed_dob or not listed_name or not util.same_party(title_party(doc), listed_name):
                continue
            stated = util.stated_births(doc.body)
            if listed_dob in util.all_dates(doc.body):
                verdict = ("same", "{} gives {}'s date of birth as {}, the same as internal list entry {} ({}, born {}); the listed reason is: {}".format(
                    doc_ref(doc), listed_name, D(listed_dob), entry_id, listed_name, D(listed_dob), (entry.get("reason") or "").rstrip(".")), [doc.id])
            elif stated:
                verdict = ("different", "{} gives the payer {}'s date of birth as {}, but internal list entry {} for that name was born {}, "
                           "so the name match is a different person".format(doc_ref(doc), listed_name, D(stated[0]), entry_id, D(listed_dob)), [doc.id])
            else:
                continue
            break

        if verdict is None:
            verdict = _register_check(ctx, alert, entry, txs)

        if verdict is None:
            ctx.notes.setdefault("list_unresolved", []).append(alert["alert_id"])
            continue
        kind, text, docs = verdict
        if kind == "same":
            if not _tx_clause(txs) or "still controlled" in text:
                full = text
            else:
                full = text + ". {}, so the money came from the listed party itself and the document on file does not clear the match".format(_tx_clause(txs))
            ctx.add("suspect", "ESC_LISTED_PARTY", full, docs + [entry_id] + tx_ids, rank=95, tag="listed")
        else:
            ctx.add("explain", "LIST_MATCH_DIFFERENT_PARTY", text, docs + [entry_id] + tx_ids, alerts=[alert["alert_id"]], rank=80, tag="list_different")


def _tx_clause(txs):
    if not txs:
        return ""
    return "The alert is on " + join_names([tx_phrase(t) for t in txs], 2)


def register_people(case, skip_customer=True):
    """People named in company register and control register tables: dicts with doc, company, name, born, role, end."""
    people = []
    for doc in case.docs("company_register_extract", "significant_control_register", "group_structure"):
        company = doc.field("Company") or title_party(doc)
        if skip_customer and util.same_party(company, case.name):
            continue
        for table in doc.tables:
            for row in table:
                name = row.get("Officer") or row.get("Person") or row.get("Person with significant control")
                if not name:
                    continue
                people.append({
                    "doc": doc, "company": company, "name": name, "born": util.parse_month_year(row.get("Born") or ""),
                    "born_text": row.get("Born") or "", "role": row.get("Role") or row.get("Control") or "",
                    "end": util.parse_date(row.get("Resigned") or row.get("Ceased") or ""),
                    "end_text": row.get("Resigned") or row.get("Ceased") or "", "is_officer": "Officer" in row,
                })
    return people


def _register_check(ctx, alert, entry, txs):
    """Company register and control register evidence about a listed person or address."""
    case = ctx.case
    m = alert.get("measured") or {}
    matched_party = m.get("matched_party") or ""
    people = [p for p in register_people(case)
              if util.mentions(matched_party, p["company"]) or any(util.party_like(p["company"], t.get("counterparty_name")) for t in txs)]
    if not people:
        return None
    listed_name = entry.get("name") or ""
    listed_dob = util.parse_date(entry.get("date_of_birth"))
    entry_id = entry.get("entry_id") or m.get("list_entry")
    paid = min((tx_date(t) for t in txs), default=None)
    company = people[0]["company"]
    docs = list(dict.fromkeys(p["doc"].id for p in people))
    support = [d for d in case.docs("receipt", "invoice") if any(has_amount(d, t["amount_gbp"]) for t in txs)]
    same_name = [p for p in people if util.same_party(p["name"], listed_name)]
    if same_name:
        with_birth = [p for p in same_name if p["born"]]
        if not listed_dob or not with_birth:
            return None
        matching = [p for p in with_birth if p["born"] == (listed_dob.year, listed_dob.month)]
        if not matching:
            p = with_birth[0]
            return ("different", "{} shows the {} of {} named {} was born {}, but internal list entry {} for that name was born {}, "
                    "so it is a different person".format(doc_ref(p["doc"]), "officer" if p["is_officer"] else "owner", company,
                                                         p["name"], p["born_text"], entry_id, D(listed_dob)), docs)
        active = [p for p in matching if p["end"] is None or (paid and p["end"] >= paid)]
        if not active:
            return None
        parts = []
        for p in matching:
            when = " until {}".format(p["end_text"]) if p["end_text"] else ""
            parts.append("{} shows {}, born {}, {}{}".format(
                p["doc"].id, p["name"], p["born_text"],
                "was a {} of {}".format(p["role"].lower(), company) if p["is_officer"] else "held control of {} ({})".format(company, p["role"]), when))
        text = "{}. Internal list entry {} is {}, born {} ({}). {} was made on {} while the listed person still controlled the company".format(
            "; ".join(parts), entry_id, listed_name, D(listed_dob), (entry.get("reason") or "").rstrip("."),
            "The payment of {}".format(M(txs[0]["amount_gbp"])) if txs else "The payment", D(paid) if paid else "the alert date")
        if support:
            text += ", so the matching {} {} does not clear the match".format(support[0].type.replace("_", " "), support[0].id)
        return ("same", text, docs + [d.id for d in support])
    # Nobody at the company has the listed person's name. That clears the match only when the register also shows
    # the company at a different address from the one on the list.
    reg = next((p["doc"] for p in people if p["doc"].field("Registered office")), None)
    office = reg.field("Registered office") if reg else ""
    listed_address = entry.get("address") or ""
    if not office or not listed_address or util.same_address(office, listed_address):
        return None
    who = []
    for p in people:
        label = "{}{}".format(p["name"], ", born " + p["born_text"] if p["born_text"] else "")
        if label not in who:
            who.append(label)
    text = "{} shows {} is registered at {} and is run and owned by {}. Internal list entry {} is {}{} at {} ({}), so the match on {} is to a different party".format(
        doc_ref(reg), company, office, join_names(who, 3), entry_id, listed_name,
        ", born " + D(listed_dob) if listed_dob else "", listed_address,
        (entry.get("reason") or "").rstrip("."), re.sub(r" in .*", "", m.get("matched_on", "one detail")))
    if support:
        text += ". {} confirms what the payment of {} was for".format(doc_ref(support[0]), M(txs[0]["amount_gbp"]) if txs else "this amount")
    return ("different", text, docs + [d.id for d in support])


# --------------------------------------------------------------------------------------------------------------
# Suspicion
# --------------------------------------------------------------------------------------------------------------

CHAT_PATTERNS = [
    # (tag, regex over what the customer said)
    ("job_forwarding", r"payments? assistant|pay into my account and I send|clients pay into my account|keep \d+% as my (wages|pay|commission)"),
    ("invest_for_others", r"invest(ing)? for (a few|some|several|other) (people|friends)|they are investing through mine|investing through my|"
                          r"cannot open an account|can't open an account"),
    ("sent_to_invest", r"sent me (the )?money to invest|money to invest for them"),
    ("loan_from_friend", r"\bloan\b.{0,60}\bfriend|friend.{0,40}\b(loan|lent|lending)\b"),
    ("family_support", r"help(ing)? my family|support(ing)? my family|family support|for my family there"),
]


def chat_tags(case):
    tags = {}
    if IGNORE_CHAT_WORDING:
        return tags
    for doc in case.docs("support_chat"):
        said = " ".join(doc.customer_turns())
        for tag, pattern in CHAT_PATTERNS:
            if re.search(pattern, said, re.I):
                tags.setdefault(tag, doc)
    return tags


def quote(doc, limit=170):
    said = " ".join(doc.customer_turns()).strip()
    return said if len(said) <= limit else said[:limit].rsplit(" ", 1)[0] + "..."


def third_party_flows(ctx):
    """(third-party inflows, third-party outflows) among the flagged transactions."""
    ids = ctx.all_flagged_ids()
    txs = [t for t in ctx.case.transactions if t["transaction_id"] in ids]
    skip = ("fund_subscription", "fund_redemption", "card", "cash_withdrawal", "cash_deposit", "cash_topup")
    ins = [t for t in txs if t["direction"] == "in" and t.get("channel") not in skip and not ctx.is_customer(t.get("counterparty_name"))]
    outs = [t for t in txs if t["direction"] == "out" and t.get("channel") not in skip and not ctx.is_customer(t.get("counterparty_name"))]
    return ins, outs


def chat_admissions(ctx):
    """What the customer told the firm shows the account is being used for other people's money."""
    case = ctx.case
    tags = chat_tags(case)
    ins, outs = third_party_flows(ctx)
    # The customer's words never decide a case alone: each admission needs the matching movement of money.
    many_payers = len({util.norm_name(t.get("counterparty_name")) for t in ins}) >= 2
    tm08 = any(a["rule_id"] == "TM08" for a in case.alerts)
    if not (many_payers and outs):
        tags.pop("job_forwarding", None)
    if not (tm08 and (outs if case.alert("TM10") else ins)):
        tags.pop("invest_for_others", None)
    if not (ins and outs) or "job_forwarding" in tags:
        tags.pop("sent_to_invest", None)
    payers = join_names([party(t) for t in ins], 3)
    in_total = sum(t["amount_gbp"] for t in ins)
    out_total = sum(t["amount_gbp"] for t in outs)
    ev_tx = [t["transaction_id"] for t in (ins[:3] + outs[:3])]
    if "job_forwarding" in tags:
        doc = tags["job_forwarding"]
        ctx.add("suspect", "ESC_MULE_ACTIVITY",
                "In {} the customer says: \"{}\" The account received {} from {} and passed it on, which is other people's money moved for someone else".format(
                    doc_ref(doc), quote(doc), M(in_total), payers or "several payers"),
                [doc.id] + ev_tx, rank=90, tag="chat_mule", brief="In {} the customer says: \"{}\"".format(doc.id, quote(doc)))
    if "invest_for_others" in tags:
        doc = tags["invest_for_others"]
        if case.alert("TM10"):
            m = case.alert("TM10").get("measured") or {}
            ctx.add("suspect", "ESC_EARLY_REDEMPTION",
                    "In {} the customer says: \"{}\" {} paid in, units in {} were bought for {} and sold back {} days later for {}, and the proceeds went to {}, "
                    "so fund units are being bought and quickly redeemed to move other people's money".format(
                        doc_ref(doc), quote(doc), payers or "A third party", m.get("fund", "a fund"), M(m.get("bought_gbp", 0)),
                        m.get("held_days", "a few"), M(m.get("redeemed_gbp", 0)), join_names([party(t) for t in outs], 2) or "a third party"),
                    [doc.id] + [t["transaction_id"] for t in ctx.flagged(case.alert("TM10"))[-2:]] + ev_tx, rank=88, tag="chat_redemption")
        else:
            countries = sorted({t.get("counterparty_country") for t in ins + outs} - {"United Kingdom", None})
            ctx.add("suspect", "ESC_THIRD_PARTY_FUNDING",
                    "In {} the customer says: \"{}\" {} totalling {} came from {}{}, people unrelated to the declared salary funding, so third parties are "
                    "using the customer's account to invest".format(
                        doc_ref(doc), quote(doc), "Deposits" if len(ins) != 1 else "A deposit", M(in_total), payers or "third parties",
                        " in " + join_names(countries) if countries else ""),
                    [doc.id] + ev_tx, rank=86, tag="chat_third_party")
    if "sent_to_invest" in tags and "invest_for_others" not in tags:
        doc = tags["sent_to_invest"]
        ctx.add("suspect", "ESC_MULE_ACTIVITY",
                "In {} the customer says: \"{}\" The account took in {} from {} and {} went out to {} instead of back to the senders, so it is receiving and "
                "passing on other people's money".format(
                    doc_ref(doc), quote(doc), M(in_total), payers or "third parties", M(out_total),
                    join_names([party(t) for t in outs], 2) or "another party"),
                [doc.id] + ev_tx, rank=89, tag="chat_mule")


def dissolved_payer(ctx):
    """Money arrives from a company the register shows was already dissolved."""
    case = ctx.case
    tags = chat_tags(case)
    for doc in case.docs("company_register_extract"):
        status = doc.field("Status") or ""
        if not re.search(r"dissolved", status, re.I):
            continue
        company = doc.field("Company") or title_party(doc)
        gone = util.parse_date(status)
        ins = [t for t in case.transactions if t["direction"] == "in" and util.same_party(t.get("counterparty_name"), company)
               and relevant(case, t) and (not gone or tx_date(t) > gone)]
        if not ins:
            continue
        first, last = ins[0]["booked_at"], ins[-1]["booked_at"]
        outs = [t for t in case.transactions if t["direction"] == "out" and t["booked_at"] >= first
                and t.get("channel") in ("faster_payment", "swift", "wallet_transfer", "bacs")
                and not ctx.is_customer(t.get("counterparty_name")) and relevant(case, t)]
        total_in = sum(t["amount_gbp"] for t in ins)
        total_out = sum(t["amount_gbp"] for t in outs)
        accounts = {t.get("counterparty_account") for t in ins if t.get("counterparty_account")}
        text = "{} shows {} was dissolved on {}, yet it paid the account {} in {} payment{} after that{} (reference \"{}\")".format(
            doc_ref(doc), company, D(gone) if gone else "an earlier date", M(total_in), len(ins), "" if len(ins) == 1 else "s",
            ", each from a different account" if len(accounts) > 1 and len(accounts) == len(ins) else "", ins[0].get("reference"))
        ev = [doc.id]
        chat = tags.get("loan_from_friend")
        if chat:
            text += ". In {} the customer calls it \"a loan from a friend\", which does not fit a company payer or that reference".format(chat.id)
            ev.append(chat.id)
        if outs:
            share = total_out / total_in if total_in else 0
            text += ". {} ({:.0%}) was then sent on to {}{}, so the funds have no credible explanation and were moved on".format(
                M(total_out), share, join_names([party(t) for t in outs], 3),
                " in " + join_names(sorted({t.get("counterparty_country") for t in outs} - {"United Kingdom", None})) if
                ({t.get("counterparty_country") for t in outs} - {"United Kingdom", None}) else "")
        ctx.add("suspect", "ESC_UNEXPLAINED_FUNDS", text, ev + [t["transaction_id"] for t in ins[:3] + outs[:3]], rank=84, tag="dissolved")
        return



INVOICE_TYPES = ("invoice", "dealer_invoice", "hospital_invoice", "supplier_invoice", "carrier_invoice")


def invoices(ctx):
    """An invoice on file matches an outgoing payment on payee and amount (and account or number where given)."""
    case = ctx.case
    for doc in case.docs(*INVOICE_TYPES):
        seller = (doc.field("Supplier", "Carrier") or title_party(doc)).split(",")[0].strip()
        billed = doc.field("Bill to", "Customer", "Buyer")
        if billed and not util.mentions(billed, case.name):
            ctx.notes.setdefault("document_names_someone_else", []).append(doc.id)
            continue
        cands = [t for t in case.transactions if t["direction"] == "out" and relevant(case, t)
                 and util.party_like(seller, t.get("counterparty_name"))]
        pay_to = doc.field("Pay to") or ""
        on_invoice = bool(accounts_in(pay_to))

        def right_account(t):
            return not on_invoice or not t.get("counterparty_account") or doc_has_account(doc, t["counterparty_account"])

        number = re.search(r"Invoice\s+((?:[A-Z]+[ /]?)?\d[\w/-]*)", doc.title) or re.search(r"\b((?:PO|INV|ORDER) ?\d+)", doc.title)
        key = re.sub(r"\W", "", number.group(1)).lower() if number else ""
        quoting = [t for t in cands if key and key in re.sub(r"\W", "", t.get("reference") or "").lower()]
        hits = [t for t in cands if has_amount(doc, t["amount_gbp"]) and right_account(t)]
        if not hits and quoting and all(right_account(t) for t in quoting) \
                and any(util.same_amount(sum(t["amount_gbp"] for t in quoting), a) for a in doc.amounts()):
            hits = quoting   # the invoice was paid in instalments that add up to it
        if hits and doc.date and any(tx_date(t) < doc.date - _days(3) for t in hits):
            ctx.notes.setdefault("invoice_dated_after_payment", []).append(doc.id)
            hits = []
        if not hits:
            if cands:
                ctx.notes.setdefault("invoice_mismatch", []).append((doc.id, [t["transaction_id"] for t in cands]))
                stated = doc.amounts()
                wrong_account = [t for t in cands if has_amount(doc, t["amount_gbp"]) and not right_account(t)]
                if wrong_account:
                    t = wrong_account[0]
                    ctx.add("suspect", "ESC_INVOICE_MISMATCH",
                            "{} from {} says to pay {}, but {} went to a different account ({}), so the invoice on file does not support the payment".format(
                                doc_ref(doc), seller, pay_to, tx_phrase(t), t.get("counterparty_account")),
                            [doc.id] + [x["transaction_id"] for x in wrong_account], rank=80, tag="invoice_mismatch")
                elif quoting and stated:
                    t = quoting[0]
                    ctx.add("suspect", "ESC_INVOICE_MISMATCH",
                            "{} from {} is for {}, but {}, which quotes that invoice, is for a different amount (a difference of {}), "
                            "so the invoice on file does not support the payment".format(
                                doc_ref(doc), seller, M(stated[0]), tx_phrase(t), M(abs(stated[0] - sum(x["amount_gbp"] for x in quoting)))),
                            [doc.id] + [x["transaction_id"] for x in quoting], rank=80, tag="invoice_mismatch")
            continue
        t = hits[0]
        ref_ok = bool(number) and re.sub(r"\W", "", number.group(1)).lower() in re.sub(r"\W", "", t.get("reference") or "").lower()
        what = doc.field("Description", "Goods", "Service", "Treatment", "Vehicle")
        patient = doc.field("Patient")
        text = "{} from {}{} is for {}{}; payee{} and amount match {}{}".format(
            doc_ref(doc), seller, " billed to " + billed if billed else "", M(t["amount_gbp"]),
            " ({}{})".format(what, ", patient " + patient if patient else "") if what else "",
            ", account" if accounts_in(pay_to) and t.get("counterparty_account") else "",
            join_names([tx_phrase(x) for x in hits], 3),
            ", whose reference \"{}\" quotes the invoice number".format(t.get("reference")) if ref_ok else
            (", reference \"{}\"".format(t.get("reference")) if t.get("reference") else ""))
        ev = [doc.id]
        for chat in case.docs("support_chat"):
            if patient and util.mentions(" ".join(chat.customer_turns()), patient):
                text += ". In {} the customer says: \"{}\"".format(chat.id, quote(chat, 200).rstrip("."))
                ev.append(chat.id)
        for other in case.docs("purchase_order", "supply_contract"):
            po = re.search(r"\bPO ?\d+", other.body)
            if other.type == "purchase_order" and po and po.group(0).replace(" ", "") in doc.raw.replace(" ", ""):
                value = other.amounts()
                text += ". {} from {} is the customer order ({}{}) those goods were bought to fill, which explains the size of the payment".format(
                    doc_ref(other), title_party(other), po.group(0), ", worth " + M(value[0]) if value else "")
                ev.append(other.id)
            elif other.type == "supply_contract" and other.mentions(seller):
                earlier = other.date and other.date <= tx_date(t)
                text += ". {} is the client's supply contract with {}{}".format(doc_ref(other), seller, ", signed before the payment" if earlier else "")
                ev.append(other.id)
        ctx.add("explain", "INVOICE_MATCHES", text, ev + [x["transaction_id"] for x in hits],
                tx_ids=[x["transaction_id"] for x in hits], rank=64, tag="invoice")


CASH_IN = ("cash_deposit", "cash_topup")


def cash_records(ctx):
    """Business cash records on file reconcile, line by line, to the cash paid in."""
    case = ctx.case
    docs = case.docs("cash_collection_reports", "cash_reconciliation", "takings_book")
    if not docs:
        return
    rows = []
    for doc in docs:
        for table in doc.tables:
            for row in table:
                cells = " | ".join(row.values())
                rows.append({"doc": doc, "amounts": util.money_values(cells), "dates": util.all_dates(cells), "row": row, "used": False})
    flagged = flagged_ids(case)
    deposits = [t for t in case.transactions if t.get("channel") in CASH_IN and t["direction"] == "in" and t["transaction_id"] in flagged]
    matched, used_docs = [], []
    for t in deposits:
        d = tx_date(t)
        for r in rows:
            if r["used"] or not any(util.same_amount(t["amount_gbp"], a) for a in r["amounts"]):
                continue
            if r["dates"] and not any(0 <= (d - rd).days <= 4 for rd in r["dates"]):
                continue
            takings = util.parse_money(r["row"].get("Takings", ""))
            if takings is not None and t["amount_gbp"] > takings + 0.005:
                continue
            r["used"] = True
            matched.append((t, r))
            if r["doc"] not in used_docs:
                used_docs.append(r["doc"])
            break
    if not matched:
        return
    if len(matched) < len(deposits):
        ctx.notes["cash_records_partial"] = [d.id for d in docs]
    support = case.docs("cash_handling_procedure", "ticket_report")
    main = used_docs[0]
    examples = []
    for t, r in matched[:3]:
        label = next((v for k, v in r["row"].items() if k in ("Site", "Point", "Market")), "")
        examples.append("{} for {} ({})".format(M(t["amount_gbp"]), label or "the day", t["transaction_id"]))
    text = "{} ({}) lists the cash counted line by line, and {} of the {} flagged cash {} match a line on amount and date: {}".format(
        doc_ref(main), main.title, len(matched), len(deposits), "top-ups" if deposits[0]["channel"] == "cash_topup" else "deposits",
        join_names(examples, 3))
    if support:
        text += ". {} explain{} why each site's cash is banked separately in amounts of this size".format(
            join_names([doc_ref(d) for d in support], 2), "s" if len(support) == 1 else "")
    ctx.add("explain", "BUSINESS_RECORDS_RECONCILE", text, [d.id for d in used_docs] + [d.id for d in support] + [t["transaction_id"] for t, _ in matched],
            tx_ids=[t["transaction_id"] for t, _ in matched], rank=66, tag="cash_records")


def cash_sale(ctx):
    """A signed sale receipt shows the customer sold something for cash, matching the cash paid in."""
    case = ctx.case
    for doc in case.docs("vehicle_sale_receipt", "sale_receipt"):
        seller = doc.field("Seller") or ""
        m = re.search(r"\bI,\s+([A-Z][\w'-]+(?: [A-Z][\w'-]+)+)", doc.body)
        if not seller and m:
            seller = m.group(1)
        if not seller:
            for ad in case.docs("advert"):
                sm = re.search(r"Seller:\s*([A-Z][\w'-]+(?: [A-Z][\w'-]+)+)", ad.body)
                if sm:
                    seller = sm.group(1)
        if not seller or not util.mentions(seller, case.name):
            ctx.notes.setdefault("customer_not_seller", []).append(doc.id)
            continue
        txs = [t for t in case.transactions if t["direction"] == "in" and t.get("channel") in CASH_IN and relevant(case, t)
               and has_amount(doc, t["amount_gbp"]) and (not doc.date or 0 <= (tx_date(t) - doc.date).days <= 21)]
        if not txs:
            continue
        buyer = doc.field("Buyer") or ""
        if not buyer:
            stop = {"Sale", "Receipt", "Signed", "Payment", "Bike", "Road", "Carbon", "Private", "Vehicle", "Dear", "Regards", "Received", "Total", "Cash"}
            names = [n for n in re.findall(r"\b[A-Z][a-z'-]+ [A-Z][a-z'-]+\b", doc.body)
                     if not util.same_party(n, case.name) and not (set(n.split()) & stop)]
            buyer = names[0] if names else ""
        thing = ""
        for m in re.finditer(r"\b(?:sale of|for|sold) (?:a |an |one |my |the )?((?:\d{4} )?[A-Za-z][A-Za-z -]+?)(?:,| \(| to | for |\.| in )", doc.body):
            found = m.group(1).strip()
            if found and not re.match(r"(sum|total|amount|cash|as seen|full|payment|sale|price)\b", found):
                thing = found
                break
        if not thing:
            for ad in case.docs("advert"):
                first = re.split(r"[,.]", " ".join(ad.prose))[0].strip()
                if first:
                    thing = first[0].lower() + first[1:]
        if not thing:
            thing = (doc.title or "").lower().replace("sale receipt", "").replace("private", "").strip()
        reg = re.search(r"registration ([A-Z]{2}\d{2} ?[A-Z]{3})", doc.body)
        text = "{} records {} selling {}{}{} for {} in cash; that matches {}".format(
            doc_ref(doc), case.name, ("a " + thing) if thing else "the item it describes",
            " (registration {})".format(reg.group(1)) if reg else "", " to " + buyer if buyer else "", M(txs[0]["amount_gbp"]),
            join_names([tx_phrase(t) for t in txs], 2))
        ev = [doc.id]
        for other in case.docs("insurance_schedule", "advert"):
            if other.type == "insurance_schedule" and reg and reg.group(1) in other.body and util.mentions(other.field("Policyholder") or "", case.name):
                text += ". {} shows the customer insured the same vehicle, so it was theirs to sell".format(doc_ref(other))
                ev.append(other.id)
            elif other.type == "advert" and util.mentions(other.body, case.name):
                asked = other.amounts()
                text += ". {} shows the customer advertised it{}".format(doc_ref(other), " at " + M(asked[0]) if asked else "")
                ev.append(other.id)
        ctx.add("explain", "DOCUMENTED_EVENT", text, ev + [t["transaction_id"] for t in txs],
                tx_ids=[t["transaction_id"] for t in txs], rank=62, tag="cash_sale")


def cash_redeposit(ctx):
    """Cash paid in is the same cash the customer withdrew earlier, as the account's own history shows."""
    case = ctx.case
    flagged = flagged_ids(case)
    for chat in case.docs("support_chat"):
        said = " ".join(chat.customer_turns())
        stated = util.money_values(said)
        if not stated:
            continue
        for t in case.transactions:
            if t["transaction_id"] not in flagged or t.get("channel") not in CASH_IN or t["direction"] != "in":
                continue
            if not any(util.same_amount(t["amount_gbp"], a) for a in stated):
                continue
            earlier = [w for w in case.transactions if w.get("channel") == "cash_withdrawal" and w["booked_at"] < t["booked_at"]
                       and util.same_amount(w["amount_gbp"], t["amount_gbp"])]
            if not earlier:
                ctx.notes["redeposit_without_withdrawal"] = chat.id
                continue
            w = earlier[-1]
            reviews = [r for r in case.reviews if re.search(r"withdraw", r.get("note", ""), re.I)]
            line = next((x for x in chat.customer_turns() if any(util.same_amount(t["amount_gbp"], a) for a in util.money_values(x))), said)
            text = "The account's own history shows {} was withdrawn in cash on {}{} ({}), and {} is the same amount paid back in. " \
                   "In {} the customer says: \"{}\"".format(
                       M(w["amount_gbp"]), D(tx_date(w)), " at " + w["location"] if w.get("location") else "", w["transaction_id"],
                       tx_phrase(t), doc_ref(chat), line.strip().rstrip("."))
            if reviews:
                text += ". Earlier review {} of {} closed that withdrawal ({}), and the amount returned is no larger".format(
                    reviews[-1]["review_id"], D(util.parse_date(reviews[-1]["reviewed_on"])), reviews[-1]["note"].rstrip("."))
            ev = [chat.id] + [r["review_id"] for r in reviews[-1:]] + [t["transaction_id"], w["transaction_id"]]
            ctx.add("explain", "REFUND_OF_EARLIER_PAYMENT", text, ev, tx_ids=[t["transaction_id"]], rank=62, tag="redeposit")


def refunds(ctx):
    """Money received is the refund of a payment the customer made earlier to the same party."""
    case = ctx.case
    for doc in case.docs("venue_email", "credit_note"):
        payer = (doc.field("Supplier") or title_party(doc)).split(",")[0].strip()
        for t in case.transactions:
            if t["direction"] != "in" or not relevant(case, t) or not has_amount(doc, t["amount_gbp"]):
                continue
            if not util.party_like(payer, t.get("counterparty_name")):
                continue
            ref = re.search(r"\b([A-Z]{3,} ?\d{3,})\b", doc.body)
            earlier = [o for o in case.transactions if o["direction"] == "out" and o["booked_at"] < t["booked_at"]
                       and util.party_like(payer, o.get("counterparty_name"))
                       and (util.same_amount(o["amount_gbp"], t["amount_gbp"]) or
                            (ref and ref.group(1).replace(" ", "") in (o.get("reference") or "").replace(" ", "")))]
            if not earlier:
                ctx.notes.setdefault("refund_without_original", []).append(doc.id)
                continue
            o = earlier[-1]
            reviews = [r for r in case.reviews if util.mentions(r.get("note", ""), payer)]
            text = "{} from {} confirms a full refund of {}{}. It matches {}, and the original payment is on the account: {}{}".format(
                doc_ref(doc), payer, M(t["amount_gbp"]), " for " + ref.group(1) if ref else "", tx_phrase(t), tx_phrase(o),
                " (reference \"{}\")".format(o.get("reference")) if o.get("reference") else "")
            if reviews:
                text += ". Earlier review {} had already closed that original payment ({})".format(reviews[-1]["review_id"], reviews[-1]["note"].rstrip("."))
            ev = [doc.id] + [r["review_id"] for r in reviews[-1:]] + [t["transaction_id"], o["transaction_id"]]
            ctx.add("explain", "REFUND_OF_EARLIER_PAYMENT", text, ev, tx_ids=[t["transaction_id"]], rank=63, tag="refund")


def group_booking(ctx):
    """A booking confirmation lists the people who paid the customer and the firm the money was passed to."""
    case = ctx.case
    for doc in case.docs("booking_confirmation"):
        travellers = [m.group(1).strip() for line in doc.prose for m in [re.match(r"\d+\.\s+(.+)$", line)] if m]
        lead = doc.field("Lead traveller") or ""
        if lead and not util.mentions(lead, case.name):
            ctx.notes.setdefault("document_names_someone_else", []).append(doc.id)
            continue
        prices = util.money_values(doc.field("Price") or "")
        per_head = prices[0] if prices else None
        total = prices[1] if len(prices) > 1 else None
        firm = title_party(doc)
        payment = doc.field("Payment") or ""
        ins = [t for t in case.transactions if t["direction"] == "in" and relevant(case, t)
               and any(util.same_party(t.get("counterparty_name"), n) for n in travellers)
               and (per_head is None or abs(t["amount_gbp"] / per_head - round(t["amount_gbp"] / per_head)) < 1e-6)]
        outs = [t for t in case.transactions if t["direction"] == "out" and relevant(case, t)
                and util.party_like(firm, t.get("counterparty_name"))
                and (util.same_amount(t["amount_gbp"], total) or doc_has_account(doc, t.get("counterparty_account")))]
        if not ins:
            continue
        ref = doc.field("Booking reference") or ""
        text = "{} from {} names {} as lead traveller for a {} with {} travellers at {} each ({} in total). All {} people who paid {} each " \
               "({} in all) are on its traveller list, for example {}".format(
                   doc_ref(doc), firm, lead or case.name, (lambda trip: trip[:1].lower() + trip[1:])((doc.field("Trip") or "group trip").split(",")[0]),
                   doc.field("Travellers") or len(travellers), M(per_head) if per_head else "the listed price", M(total) if total else "the listed total",
                   len({util.norm_name(t["counterparty_name"]) for t in ins}), M(ins[0]["amount_gbp"]),
                   M(sum(t["amount_gbp"] for t in ins)), join_names([t["counterparty_name"] for t in ins], 3))
        if outs:
            text += ". The customer then paid {} to {} on {} ({}), the payee{} named on the booking{}".format(
                M(outs[0]["amount_gbp"]), party(outs[0]), D(tx_date(outs[0])), outs[0]["transaction_id"],
                " and account" if doc_has_account(doc, outs[0].get("counterparty_account")) else "",
                ", reference " + ref if ref else "")
        ids = [t["transaction_id"] for t in ins + outs]
        ctx.add("explain", "COLLECTION_DOCUMENTED", text, [doc.id] + [t["transaction_id"] for t in outs] + [t["transaction_id"] for t in ins],
                tx_ids=ids, rank=65, tag="collection")



def _key(text):
    return re.sub(r"\W", "", text or "").lower()


def invoice_tables(ctx):
    """Schedules of invoices, contract stages or payees on file, checked line by line against the payments."""
    case = ctx.case
    used = ctx.explained_tx()

    for doc in case.docs("supplier_invoices"):
        supplier = (doc.field("Supplier") or title_party(doc)).split(",")[0].strip()
        outs = [t for t in case.transactions if t["direction"] == "out" and relevant(case, t)
                and util.party_like(supplier, t.get("counterparty_name"))]
        matched, wrong = [], []
        for table in doc.tables:
            for row in table:
                amount = util.parse_money(row.get("Amount", ""))
                number = row.get("Invoice") or ""
                quoting = [t for t in outs if _key(number) and _key(number) in _key(t.get("reference"))]
                exact = [t for t in (quoting or outs) if util.same_amount(t["amount_gbp"], amount) and t not in [x for _, x in matched]]
                if exact:
                    matched.append((row, exact[0]))
                elif quoting:
                    wrong.append((row, quoting[0]))
        if wrong:
            bits = []
            for row, t in wrong[:2]:
                amount = util.parse_money(row.get("Amount", "")) or 0
                bits.append("{} is for {} ({}) but the payment quoting it was {} on {} ({}), a difference of {}".format(
                    row.get("Invoice"), M(amount), row.get("Goods") or "goods", M(t["amount_gbp"]), D(tx_date(t)), t["transaction_id"], M(abs(amount - t["amount_gbp"]))))
            text = "{} from {} does not support the payments: {}".format(doc_ref(doc), supplier, "; ".join(bits))
            late = [row for row, t in wrong if util.parse_date(row.get("Dated", "")) and util.parse_date(row.get("Dated", "")) > tx_date(t)]
            if late:
                text += ". {} dated after the payment {} meant to support".format(
                    "The invoice is" if len(late) == 1 else "{} invoices are".format(len(late)), "it is" if len(late) == 1 else "they are")
            goods = wrong[0][0].get("Goods")
            if goods and case.customer.get("business_activity"):
                text += ". The goods ({}) do not fit the client's declared business ({})".format(goods, case.customer["business_activity"].rstrip("."))
            ctx.add("suspect", "ESC_INVOICE_MISMATCH", text, [doc.id] + [t["transaction_id"] for _, t in wrong], rank=86, tag="invoice_mismatch")
        elif matched:
            txs = [t for _, t in matched]
            text = "{} from {} lists {} invoice{} ({}) and each matches a payment on amount and number: {}".format(
                doc_ref(doc), supplier, len(matched), "" if len(matched) == 1 else "s", matched[0][0].get("Goods") or "goods",
                join_names(["{} for {} ({})".format(r.get("Invoice"), M(t["amount_gbp"]), t["transaction_id"]) for r, t in matched], 3))
            ctx.add("explain", "INVOICE_MATCHES", text, [doc.id] + [t["transaction_id"] for t in txs], tx_ids=[t["transaction_id"] for t in txs], rank=64, tag="invoice")

    for doc in case.docs("freight_invoices"):
        matched = []
        for table in doc.tables:
            for row in table:
                amount = util.parse_money(row.get("Amount", ""))
                payer = row.get("Exporter") or row.get("Customer") or row.get("Bill to") or ""
                hit = next((t for t in case.transactions if t["direction"] == "in" and relevant(case, t)
                            and util.party_like(payer, t.get("counterparty_name")) and util.same_amount(t["amount_gbp"], amount)
                            and t not in [x for _, x in matched]), None)
                if hit:
                    matched.append((row, hit))
        if matched:
            txs = [t for _, t in matched]
            text = "{} ({}) lists the invoices the client raised, and each receipt matches one on payer, amount and number: {}".format(
                doc_ref(doc), doc.title, join_names(["{} to {} for {} ({})".format(r.get("Invoice"), r.get("Exporter") or "the payer", M(t["amount_gbp"]), t["transaction_id"])
                                                    for r, t in matched], 3))
            ctx.add("explain", "INVOICE_MATCHES", text, [doc.id] + [t["transaction_id"] for t in txs], tx_ids=[t["transaction_id"] for t in txs], rank=63, tag="invoice")

    for doc in case.docs("contract_schedule"):
        supplier = (doc.field("Supplier") or title_party(doc)).split(",")[0].strip()
        outs = [t for t in case.transactions if t["direction"] == "out" and relevant(case, t)
                and util.party_like(supplier, t.get("counterparty_name"))]
        matched = []
        for table in doc.tables:
            for row in table:
                amount = util.parse_money(row.get("Amount", ""))
                hit = next((t for t in outs if util.same_amount(t["amount_gbp"], amount) and t not in [x for _, x in matched]), None)
                if hit:
                    matched.append((row, hit))
        if matched and doc.date and min(tx_date(t) for _, t in matched) < doc.date:
            ctx.notes.setdefault("schedule_dated_after_payment", []).append(doc.id)
            matched = []
        if matched:
            txs = [t for _, t in matched]
            due_ok = all(util.parse_date(r.get("Due", "")) == tx_date(t) for r, t in matched if util.parse_date(r.get("Due", "")))
            text = "{} is the payment schedule for the client's contract with {}{}, drawn up before the first payment. Its {} stages match the {} payments on amount{}: {}".format(
                doc_ref(doc), supplier, " (" + doc.field("Project") + ")" if doc.field("Project") else "", len(matched), len(txs),
                " and due date" if due_ok else "",
                join_names(["stage {} {} ({})".format(r.get("Stage"), M(t["amount_gbp"]), t["transaction_id"]) for r, t in matched], 4))
            text += ". The payments are under the threshold because the contract stages are, not because one sum was split"
            ctx.add("explain", "INVOICE_MATCHES", text, [doc.id] + [t["transaction_id"] for t in txs], tx_ids=[t["transaction_id"] for t in txs], rank=64, tag="invoice")

    for doc in case.docs("payment_file"):
        rows = [row for table in doc.tables for row in table]
        matched = []
        for row in rows:
            name = row.get("Contractor") or row.get("Payee") or row.get("Name") or ""
            amount = util.parse_money(row.get("Amount", ""))
            hit = next((t for t in case.transactions if t["direction"] == "out" and relevant(case, t)
                        and util.same_party(name, t.get("counterparty_name")) and util.same_amount(t["amount_gbp"], amount)
                        and t not in [x for _, x in matched]), None)
            if hit:
                matched.append((row, hit))
        if not matched:
            continue
        txs = [t for _, t in matched]
        addresses = [row.get("Address") for row, _ in matched if row.get("Address")]
        ins, _ = third_party_flows(ctx)
        total = sum(t["amount_gbp"] for t in txs)
        if addresses and len(set(addresses)) * 3 <= len(addresses) and ins:
            countries = sorted({row.get("Country") for row, _ in matched if row.get("Country")})
            funder = ins[0]
            text = "{} ({}) lists {} payees as being in {} but gives each a UK address, and they share only {} addresses (for example {}). " \
                   "The {} paid to them was funded by {} (reference \"{}\"), a company with no documented link to the client's business ({}), " \
                   "so the account is receiving and passing on money for someone else".format(
                       doc_ref(doc), doc.title, len(matched), join_names(countries, 3), len(set(addresses)), addresses[0], M(total),
                       tx_phrase(funder), funder.get("reference"), (case.customer.get("business_activity") or "").rstrip("."))
            ctx.add("suspect", "ESC_MULE_ACTIVITY", text, [doc.id, funder["transaction_id"]] + [t["transaction_id"] for t in txs[:4]], rank=88, tag="payment_file_mule")
        else:
            countries = sorted({row.get("Country") for row, _ in matched if row.get("Country")})
            text = "{} ({}) lists {} payees with their contract numbers and amounts, and {} of the flagged payments match it on name and amount ({} in total{}), for example {}. " \
                   "That fits the client's declared business ({})".format(
                       doc_ref(doc), doc.title, len(rows), len(matched), M(total), ", payees in " + join_names(countries, 4) if countries else "",
                       join_names(["{} {} ({})".format(r.get("Contractor") or r.get("Payee"), M(t["amount_gbp"]), t["transaction_id"]) for r, t in matched[:2]], 2),
                       (case.customer.get("business_activity") or "").rstrip("."))
            ctx.add("explain", "INVOICE_MATCHES", text, [doc.id] + [t["transaction_id"] for t in txs[:8]], tx_ids=[t["transaction_id"] for t in txs], rank=64, tag="invoice")


def group_company(ctx):
    """A group structure chart on file shows the payer and the customer are in the same group."""
    case = ctx.case
    for doc in case.docs("group_structure"):
        pairs = []
        for table in doc.tables:
            for row in table:
                company = row.get("Company") or ""
                owner = row.get("Owned by") or row.get("Owner") or ""
                if company and owner:
                    pairs.append((company, owner, row.get("Share") or ""))
        my_owners = [o for c, o, _ in pairs if util.same_party(c, case.name)]
        related = {}
        for c, o, share in pairs:
            if util.same_party(c, case.name):
                related[util.norm_name(o)] = "{} owns {} of {}".format(o, share or "the shares", c)
            if util.same_party(o, case.name):
                related[util.norm_name(c)] = "{} owns {} of {}".format(o, share or "the shares", c)
            if any(util.same_party(o, mine) for mine in my_owners) and not util.same_party(c, case.name):
                related[util.norm_name(c)] = "{} and {} are both owned {} by {}".format(c, case.name, share or "", o).replace("  ", " ")
        ins = [t for t in case.transactions if t["direction"] == "in" and relevant(case, t)
               and util.norm_name(t.get("counterparty_name")) in related and not ctx.is_customer(t.get("counterparty_name"))]
        if not ins:
            continue
        link = related[util.norm_name(ins[0]["counterparty_name"])]
        text = "{} shows {}, so {} came from a company in the client's own group{}".format(
            doc_ref(doc), link, join_names([tx_phrase(t) for t in ins], 2),
            " (the chart predates the payment)" if doc.date and doc.date <= tx_date(ins[0]) else "")
        ev = [doc.id]
        for mail in case.docs("email", "customer_email", "customer_letter"):
            if mail.mentions(ins[0]["counterparty_name"]):
                para = next((x for x in mail.prose if util.mentions(x, ins[0]["counterparty_name"])), "")
                line = next((x for x in re.split(r"(?<=[.!?])\s+", para) if util.mentions(x, ins[0]["counterparty_name"])), para)
                text += ". {} from {} says: \"{}\"".format(doc_ref(mail), title_party(mail) or "the client", line.strip().rstrip("."))
                ev.append(mail.id)
        ctx.add("explain", "GROUP_COMPANY", text, ev + [t["transaction_id"] for t in ins], tx_ids=[t["transaction_id"] for t in ins], rank=67, tag="group")


def own_funding(ctx):
    """Where clients fund the account from their own bank, the funding behind an explained payment is explained with it."""
    if not ctx.inst.funding_is_product:
        return
    case = ctx.case
    flagged = flagged_ids(case)
    spent = set()   # payments already attributed to a funding receipt
    for f in ctx.explains():
        outs = sorted((case.tx_by_id[i] for i in f.tx_ids if i in case.tx_by_id and case.tx_by_id[i]["direction"] == "out"),
                      key=lambda o: (o["booked_at"], o["transaction_id"]))
        if not outs:
            continue
        for t in case.transactions:
            if t["direction"] != "in" or t["transaction_id"] not in flagged or t["transaction_id"] in ctx.explained_tx():
                continue
            if not ctx.is_customer(t.get("counterparty_name")):
                continue
            paying = [o for o in outs if o["transaction_id"] not in spent and o["booked_at"] >= t["booked_at"]
                      and 0 <= (tx_date(o) - tx_date(t)).days <= 5]
            paid = sum(o["amount_gbp"] for o in paying)
            if paying and 0.8 * t["amount_gbp"] <= paid <= 1.05 * t["amount_gbp"]:
                spent |= {o["transaction_id"] for o in paying}
                f.tx_ids.add(t["transaction_id"])
                f.evidence.append(t["transaction_id"])
                f.text += ". The client's own funding of {} on {} ({}) paid for it".format(M(t["amount_gbp"]), D(tx_date(t)), t["transaction_id"])


PROFILE_FIELD = re.compile(r"\b(declared|expected|updated|revised|new|current)\b.*\b(income|turnover|volume)\b", re.I)


def updated_profile(ctx):
    """A later document on file gives an updated expected income or volume."""
    case = ctx.case
    alert = case.alert("TM01")
    if not alert:
        return
    old = (alert.get("measured") or {}).get("baseline_gbp") or 0
    onboarded = util.parse_date(case.customer.get("onboarded_on"))
    best = None
    for doc in case.documents:
        if not doc.date or (onboarded and (doc.date - onboarded).days < 30):
            continue   # the onboarding record is the profile the alert was measured against
        for label, value in doc.fields.items():
            amount = util.parse_money(value)
            if amount is None or not PROFILE_FIELD.search(label):
                continue
            yearly = re.search(r"annual|year|p\.a\.", label + " " + value, re.I) or \
                (re.search(r"turnover|volume", label, re.I) and not re.search(r"month", label + " " + value, re.I))
            monthly = amount / 12.0 if yearly else amount
            if monthly > old * 1.05 and (best is None or doc.date > best["doc"].date):
                best = {"monthly": monthly, "doc": doc, "label": label, "value": value}
    if best:
        ctx.notes["updated_profile"] = best


def held_payment_needs_invoice(ctx):
    """Policy: a payment at or over the threshold to anyone other than the client needs an invoice or contract on file."""
    case, limit = ctx.case, ctx.inst.invoice_threshold
    held = case.held_payment
    if not limit or not held or held["amount_gbp"] < limit or ctx.is_customer(held.get("counterparty_name")):
        return
    for f in ctx.explains():
        if held["transaction_id"] in f.tx_ids and f.code in ("INVOICE_MATCHES", "OWN_ACCOUNT"):
            return
    ctx.blocks.append({
        "tx": held,
        "fact": "The held payment, {}, is {} or more and goes to someone other than the client, so policy needs an invoice or contract on file before it is released, and none on file matches it".format(
            tx_phrase(held), M(limit)),
        "ask": "the invoice or contract from {} for {} (reference \"{}\")".format(party(held), M(held["amount_gbp"]), held.get("reference") or "none"),
    })


# --------------------------------------------------------------------------------------------------------------
# More suspicion
# --------------------------------------------------------------------------------------------------------------

def fraud_reports(ctx):
    """Other banks report that their customers were scammed into paying this account."""
    case = ctx.case
    reports = case.docs("sending_bank_fraud_report")
    if not reports:
        return
    bits, tx_ev = [], []
    for doc in reports:
        bank = title_party(doc)
        m = re.search(r"Our customer ([A-Z][\w'-]+(?: [A-Z][\w'-]+)+) sent (£[\d,.]+\d)", doc.body)
        victim, amount = (m.group(1), util.parse_money(m.group(2))) if m else (None, None)
        hit = next((t for t in case.transactions if t["direction"] == "in" and victim and util.same_party(t.get("counterparty_name"), victim)
                    and util.same_amount(t["amount_gbp"], amount)), None)
        if hit:
            tx_ev.append(hit["transaction_id"])
        bits.append("{} ({}) says its customer {} was scammed into sending {}{}".format(
            doc.id, bank, victim or "a payer", M(amount) if amount else "a payment", " (" + hit["transaction_id"] + ")" if hit else ""))
    ins, outs = third_party_flows(ctx)
    text = "{}. In all {} different people paid in {}, and {} was sent straight on to {}".format(
        "; ".join(bits), len({util.norm_name(t["counterparty_name"]) for t in ins}), M(sum(t["amount_gbp"] for t in ins)),
        M(sum(t["amount_gbp"] for t in outs)), join_names([party(t) for t in outs], 2) or "other parties")
    ctx.add("suspect", "ESC_MULE_ACTIVITY", text, [d.id for d in reports] + tx_ev + [t["transaction_id"] for t in outs[:2]], rank=92, tag="fraud_report")


def shared_device(ctx):
    """Unrelated people pay in, the money is forwarded at once, and the device is shared with other customers."""
    case = ctx.case
    if not case.alert("TM02"):
        return
    for doc in case.docs("device_export"):
        others, countries, device = set(), set(), ""
        for table in doc.tables:
            for row in table:
                device = row.get("Device") or device
                for key, value in row.items():
                    if "ustomer" in key:
                        others |= {v.strip() for v in re.split(r"[,;]", value) if v.strip()} - {case.customer.get("customer_id")}
                    if "countr" in key.lower():
                        countries |= {v.strip() for v in value.split(",") if v.strip()} - {"United Kingdom"}
        ins, outs = third_party_flows(ctx)
        payers = {util.norm_name(t["counterparty_name"]) for t in ins}
        if len(payers) < 2 or not outs or not (others or countries):
            continue
        total_in, total_out = sum(t["amount_gbp"] for t in ins), sum(t["amount_gbp"] for t in outs)
        text = "{} unrelated people ({}) paid in {} and {} ({:.0%}) was forwarded to {} within hours of each receipt. {} shows the device {} is{}{}, " \
               "so the account is receiving and passing on other people's money".format(
                   len(payers), join_names([t["counterparty_name"] for t in ins], 3), M(total_in), M(total_out), total_out / total_in if total_in else 0,
                   join_names([party(t) for t in outs], 2), doc_ref(doc), device,
                   " also used by {} other customer{} ({})".format(len(others), "" if len(others) == 1 else "s", ", ".join(sorted(others)[:3])) if others else "",
                   "{} logged in from {}".format(" and" if others else "", join_names(sorted(countries))) if countries else "")
        seen = ("{} shows the device {} is{}{}".format(
            doc.id, device, " also used by {} other customer{}".format(len(others), "" if len(others) == 1 else "s") if others else "",
            "{} logged in from {}".format(" and" if others else "", join_names(sorted(countries))) if countries else ""))
        ctx.add("suspect", "ESC_MULE_ACTIVITY", text, [doc.id] + [t["transaction_id"] for t in ins[:3] + outs[:3]], rank=87, tag="shared_device", brief=seen)
        return


def staff_notes(ctx):
    """A member of staff has put a concern about the activity on file. The rule that fired says what kind."""
    case = ctx.case
    for doc in case.docs("staff_note"):
        body = " ".join(doc.prose)
        sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", body) if x.strip()]
        if not sentences:
            continue
        asked = next((x for x in sentences if re.search(r"\b(under|below|beneath|less than|not exceed\w*)\b", x, re.I)
                      and re.search(r"£\s?[\d,]+|threshold|limit", x, re.I)), None)
        refused = next((x for x in sentences if re.search(
            r"refus|declin|would not|wouldn't|unable to|could not|couldn't|did not (explain|provide|give)|no (clear )?(explanation|justification|rationale)|"
            r"not say|evasive|unwilling|failed to", x, re.I)), None)
        however = next((x for x in sentences if re.match(r"(However|But)\b", x)), None)
        key = asked or refused or however or sentences[-1]
        tm03, tm06 = case.alert("TM03"), case.alert("TM06")
        if tm03:
            m = tm03.get("measured") or {}
            txs = ctx.flagged(tm03)
            where = {t.get("location") for t in txs if t.get("location")}
            to = m.get("beneficiary")
            text = "{} records: \"{}\" The account shows {} transactions just under the limit totalling {} within 10 days{}, " \
                   "so the amounts were split to stay under the threshold".format(
                       doc_ref(doc), key, m.get("count") or m.get("payments") or len(txs), M(m.get("total_gbp", sum(t["amount_gbp"] for t in txs))),
                       " across {} different locations".format(len(where)) if len(where) > 1 else (" to " + to if to else ""))
            for inv in case.docs("invoice"):
                total = inv.amounts()
                if total and util.same_amount(total[0], sum(t["amount_gbp"] for t in txs)):
                    text += ". {} is a single invoice for {}, the exact sum of the {} payments".format(doc_ref(inv), M(total[0]), len(txs))
            ctx.add("suspect", "ESC_STRUCTURING", text, [doc.id] + [d.id for d in case.docs("invoice", "management_accounts")] + [t["transaction_id"] for t in txs[:6]],
                    rank=91, tag="structuring")
        elif tm06:
            m = tm06.get("measured") or {}
            txs = ctx.flagged(tm06)
            text = "{} records: \"{}\" {} went to {} in {}, a higher-risk country the client did not declare, and the routing has not been explained".format(
                doc_ref(doc), key, M(m.get("total_gbp", 0)), join_names([party(t) for t in txs], 2), m.get("countries", "a higher-risk country"))
            ev = [doc.id]
            for inv in case.docs("supplier_invoices", "supplier_invoice", "invoice"):
                goods = [row.get("Goods") for table in inv.tables for row in table if row.get("Goods")] or [inv.field("Goods", "Description")]
                goods = [g for g in goods if g]
                if goods:
                    text += ". The invoices on file ({}) describe \"{}\", which does not fit the client's declared business ({})".format(
                        inv.id, goods[0], (case.customer.get("business_activity") or "").rstrip("."))
                    ev.append(inv.id)
                    break
            ctx.add("suspect", "ESC_HIGH_RISK_CORRIDOR", text, ev + [t["transaction_id"] for t in txs[:4]], rank=86, tag="corridor_note")
        else:
            txs = [case.tx_by_id[i] for i in sorted(flagged_ids(case)) if i in case.tx_by_id]
            txs.sort(key=lambda t: -t["amount_gbp"])
            text = "{} records a concern raised by staff: \"{}\" No document on file answers it, so the alerted activity ({}) is unexplained".format(
                doc_ref(doc), key, join_names([tx_phrase(t) for t in txs[:2]], 2))
            ctx.add("suspect", "ESC_UNEXPLAINED_FUNDS", text, [doc.id] + [t["transaction_id"] for t in txs[:4]], rank=70, tag="staff_concern")


SOURCE_CLAIMS = [("a gift", r"\bgift\b"), ("a sale", r"\b(selling|sold|sale of)\b"), ("a loan", r"\b(loan|lent|borrow\w*)\b"),
                 ("savings", r"\bsavings\b"), ("wages", r"\b(wages|salary)\b")]


def cash_moved_on(ctx):
    """Cash is paid in and most of it is sent on to someone else at once, with shifting explanations."""
    case = ctx.case
    tm02 = case.alert("TM02")
    if not tm02:
        return
    covered = ctx.explained_tx()
    flagged = ctx.flagged(tm02)
    cash = [t for t in flagged if t["direction"] == "in" and t.get("channel") in CASH_IN and t["transaction_id"] not in covered]
    outs = [t for t in flagged if t["direction"] == "out" and t.get("channel") in ("swift", "faster_payment", "wallet_transfer")
            and not ctx.is_customer(t.get("counterparty_name")) and t["transaction_id"] not in covered]
    chats = case.docs("support_chat")
    if not cash or not outs or not chats:
        return
    claims = []
    for chat in chats:
        said = " ".join(chat.customer_turns())
        for label, pattern in SOURCE_CLAIMS:
            if re.search(pattern, said, re.I) and label not in claims:
                claims.append(label)
    total_in, total_out = sum(t["amount_gbp"] for t in cash), sum(t["amount_gbp"] for t in outs)
    places = {t.get("location") for t in cash if t.get("location")}
    abroad = sorted({t.get("counterparty_country") for t in outs} - {"United Kingdom", None})
    text = "{} in cash was paid in{} ({}) and {} ({:.0%}) was sent to {}{} within 48 hours (reference \"{}\")".format(
        M(total_in), " at {} branches".format(len(places)) if len(places) > 1 else "", ", ".join(t["transaction_id"] for t in cash[:3]),
        M(total_out), total_out / total_in if total_in else 0, join_names([party(t) for t in outs], 2),
        " in " + join_names(abroad) if abroad else "", outs[0].get("reference"))
    if len(claims) >= 2:
        text += ". The customer gave different accounts of the cash in {}: {}".format(
            join_names([c.id for c in chats], 3), join_names(claims, 3).replace(", ", " then ").replace(" and ", " then "))
    else:
        text += ". The only explanation is the customer's own words in {} (\"{}\")".format(chats[0].id, quote(chats[0], 110))
    text += ". No document supports any of it, against a declared monthly income of {}".format(M(case.declared.get("monthly_income_gbp") or 0))
    ctx.add("suspect", "ESC_UNEXPLAINED_FUNDS", text, [c.id for c in chats] + [t["transaction_id"] for t in cash[:3] + outs[:2]], rank=83, tag="cash_moved_on")


def linked_party_pass_through(ctx):
    """Money comes in and leaves at once to companies owned by the customer's own director."""
    case = ctx.case
    if not case.is_business:
        return
    own = set()
    for doc in case.docs("company_register_extract"):
        if util.same_party(doc.field("Company") or title_party(doc), case.name):
            for table in doc.tables:
                for row in table:
                    name = row.get("Officer") or row.get("Person with significant control") or row.get("Person")
                    if name:
                        own.add(util.norm_name(name))
    if not own:
        return
    linked = {}
    for p in register_people(case):
        if util.norm_name(p["name"]) in own:
            linked.setdefault(util.norm_company(p["company"]), (p["company"], p["doc"], p["name"]))
    if not linked:
        return
    outs = [t for t in case.transactions if t["direction"] == "out" and relevant(case, t)
            and util.norm_company(t.get("counterparty_name")) in linked]
    if not outs:
        return
    first = min(t["booked_at"] for t in outs)
    flagged = flagged_ids(case)
    ins = [t for t in case.transactions if t["direction"] == "in" and t["transaction_id"] in flagged and t["booked_at"] <= first
           and t.get("channel") in ("faster_payment", "swift") and not ctx.is_customer(t.get("counterparty_name"))
           and (util.parse_datetime(first) - util.parse_datetime(t["booked_at"])).days <= 10]
    total_in, total_out = sum(t["amount_gbp"] for t in ins), sum(t["amount_gbp"] for t in outs)
    docs = [linked[util.norm_company(t["counterparty_name"])][1] for t in outs]
    director = linked[util.norm_company(outs[0]["counterparty_name"])][2]
    incs = [d.field("Incorporated") for d in docs if d.field("Incorporated")]
    text = "{} was paid out to {} ({}){}. {} show{} {} owned and directed by {}, the {}'s own director{}".format(
        M(total_out), join_names([party(t) for t in outs], 3), ", ".join(t["transaction_id"] for t in outs[:3]),
        ", {:.0%} of the {} received just before from {} (reference \"{}\")".format(
            total_out / total_in, M(total_in), join_names([party(t) for t in ins], 3), ins[0].get("reference")) if ins and total_in else "",
        join_names(list(dict.fromkeys(d.id for d in docs)), 3), "s" if len(set(d.id for d in docs)) == 1 else "",
        "it is" if len(set(d.id for d in docs)) == 1 else "they are", director, "client" if ctx.inst.name == "payments_fx" else "customer",
        ", and only incorporated on {}".format(join_names(incs, 3)) if incs else "")
    ev = list(dict.fromkeys(d.id for d in docs))
    contracts = case.docs("consulting_contract")
    if contracts and ins:
        c = contracts[0]
        client = c.field("Client") or ""
        if not any(util.party_like(client, t.get("counterparty_name")) for t in ins):
            text += ". The only contract on file, {}, is a {} job for {}, not for any of these payers, so the money passed through to linked parties with no credible purpose".format(
                c.id, c.field("Fee") or "small", client or "another client")
            ev.append(c.id)
    ctx.add("suspect", "ESC_PASS_THROUGH", text, ev + [t["transaction_id"] for t in ins[:3] + outs[:3]], rank=85, tag="pass_through")


def third_party_payout(ctx):
    """Where withdrawals must go to the customer's own account, money from one outsider paid out to another is a red flag."""
    case = ctx.case
    if ctx.suspicions():
        return
    tm08 = [a for a in case.alerts if a["rule_id"] == "TM08"]
    if not tm08 or ctx.inst.funding_is_product:
        return
    covered = ctx.explained_tx()
    flagged = [t for a in tm08 for t in ctx.flagged(a)]
    outs = [t for t in flagged if t["direction"] == "out" and not ctx.is_customer(t.get("counterparty_name")) and t["transaction_id"] not in covered]
    if not outs:
        return
    ins, _ = third_party_flows(ctx)
    ins = [t for t in ins if t["transaction_id"] not in covered and t["booked_at"] <= outs[-1]["booked_at"]]
    chat = next(iter(case.docs("support_chat")), None)
    tm10 = case.alert("TM10")
    total_in, total_out = sum(t["amount_gbp"] for t in ins), sum(t["amount_gbp"] for t in outs)
    said = " Asked about it, the customer said: \"{}\" ({}).".format(quote(chat), chat.id) if chat else ""
    ev = ([chat.id] if chat else []) + [t["transaction_id"] for t in ins[:3] + outs[:3]]
    if tm10:
        m = tm10.get("measured") or {}
        text = "{} paid in {}, units in {} were bought for {} and sold back {} days later for {}, and {} was paid out to {}, who is not the customer.{} " \
               "No document explains either party, so fund units are being bought and quickly redeemed to move money between other people".format(
                   join_names([party(t) for t in ins], 2) or "A third party", M(total_in) if ins else "the money", m.get("fund", "a fund"),
                   M(m.get("bought_gbp", 0)), m.get("held_days", "a few"), M(m.get("redeemed_gbp", 0)), M(total_out), join_names([party(t) for t in outs], 2), said)
        ctx.add("suspect", "ESC_EARLY_REDEMPTION", text, ev + [t["transaction_id"] for t in ctx.flagged(tm10)[-2:]], rank=78, tag="payout_structural")
        return
    code = "ESC_MULE_ACTIVITY" if len({util.norm_name(t["counterparty_name"]) for t in ins}) >= 2 else "ESC_UNEXPLAINED_FUNDS"
    text = "{} came in from {} and {} was paid out to {}, who is not the customer, instead of being invested or returned to the senders.{} " \
           "No document on file explains the payers or the payee".format(
               M(total_in) if ins else "Money", join_names([party(t) for t in ins], 3) or "outside parties", M(total_out),
               join_names([party(t) for t in outs], 2), said)
    ctx.add("suspect", code, text, ev, rank=77, tag="payout_structural")


def third_party_corridor_funding(ctx):
    """People other than the customer, in a higher-risk country, fund the account and nothing on file explains them."""
    case = ctx.case
    if ctx.suspicions():
        return
    tm06 = case.alert("TM06")
    tm08 = [a for a in case.alerts if a["rule_id"] == "TM08"]
    if not tm06 or not tm08 or ctx.inst.funding_is_product:
        return
    covered = ctx.explained_tx()
    ins = [t for t in ctx.flagged(tm06) if t["direction"] == "in" and not ctx.is_customer(t.get("counterparty_name"))
           and t["transaction_id"] not in covered and t.get("counterparty_country") in ctx.inst.higher_risk_countries]
    if not ins:
        return
    chat = next(iter(case.docs("support_chat")), None)
    countries = sorted({t.get("counterparty_country") for t in ins})
    text = "{} totalling {} came from {} in {}, a higher-risk country the customer did not declare, and none of them is the customer.{} " \
           "No document shows who they are or where the money came from, so unrelated third parties are funding the account".format(
               "Deposits" if len(ins) > 1 else "A deposit", M(sum(t["amount_gbp"] for t in ins)), join_names([party(t) for t in ins], 3),
               join_names(countries), " Asked about it, the customer said: \"{}\" ({}).".format(quote(chat), chat.id) if chat else "")
    ctx.add("suspect", "ESC_THIRD_PARTY_FUNDING", text, ([chat.id] if chat else []) + [t["transaction_id"] for t in ins[:5]], rank=76, tag="corridor_funding_structural")


CONTROL_PATTERNS = (r"\b(uses?|using|has access to|logs? in(to)?|controls?|runs?|manages?|operates?) my (account|app|card|phone|login)|"
                    r"\blet (him|her|them|my \w+|a \w+) use my|\bgave (him|her|them|my \w+|a \w+) my (login|log-in|password|pin|card|details|phone)|"
                    r"\b(wasn't|was not|isn't|is not) me\b|\bsomeone else (uses|has|made|did|set|does)|"
                    r"\bmy (boyfriend|girlfriend|partner|husband|wife|friend|cousin|brother|sister|boss|manager|landlord|carer) "
                    r"(does|handles|makes|did|made|set|sends|sent|moves|moved|looks after)")


def device_facts(doc, case):
    """(device, other customers who used it, countries it logged in from outside those the customer declared)."""
    device, others, countries = "", set(), set()
    declared = set(case.declared.get("expected_countries") or ["United Kingdom"])
    for table in doc.tables:
        for row in table:
            device = row.get("Device") or device
            for key, value in row.items():
                if "ustomer" in key:
                    others |= {v.strip() for v in re.split(r"[,;]", value) if v.strip()} - {case.customer.get("customer_id")}
                if "countr" in key.lower():
                    countries |= {v.strip() for v in value.split(",") if v.strip()} - declared
    return device, others, countries


def account_control(ctx):
    """Someone other than the customer appears to be operating the account, and the activity is otherwise unexplained."""
    case = ctx.case
    if ctx.suspicions():
        return
    open_alerts = [a for a in case.alerts if not alert_status(ctx, a)[0]]
    if not open_alerts:
        return   # a shared device or a remark is context only; it does not reopen activity the documents explain
    signs, ev = [], []
    for doc in case.docs("device_export"):
        device, others, countries = device_facts(doc, case)
        if others or countries:
            signs.append("{} shows the device {} used on this account is{}{}".format(
                doc_ref(doc), device or "on file",
                " also used by {} other customer{} ({})".format(len(others), "" if len(others) == 1 else "s", ", ".join(sorted(others)[:3])) if others else "",
                "{} logged in from {}".format(" and" if others else "", join_names(sorted(countries))) if countries else ""))
            ev.append(doc.id)
    if not IGNORE_CHAT_WORDING:
        for chat in case.docs("support_chat"):
            said = " ".join(chat.customer_turns())
            if re.search(CONTROL_PATTERNS, said, re.I):
                signs.append("in {} the customer says: \"{}\"".format(doc_ref(chat), quote(chat)))
                ev.append(chat.id)
    if not signs:
        return
    txs = []
    for a in open_alerts:
        for t in sorted(alert_status(ctx, a)[1] or ctx.flagged(a), key=lambda t: (-t["amount_gbp"], t["transaction_id"])):
            if t not in txs:
                txs.append(t)
    lead = "; ".join(signs)
    text = "{}. That points to someone other than {} controlling or using the account, and no document on file explains the alerted activity ({})".format(
        lead[0].upper() + lead[1:], case.name, join_names([tx_phrase(t) for t in txs[:2]], 2) or ", ".join(a["rule_id"] for a in open_alerts))
    ctx.add("suspect", "ESC_ACCOUNT_MISUSE", text, ev + [t["transaction_id"] for t in txs[:5]], rank=74, tag="account_control")


def corridor_fan_out(ctx):
    """Money is sent to several different people in a higher-risk country with nothing to show who they are."""
    case = ctx.case
    alert = case.alert("TM06")
    if not alert:
        return
    covered = ctx.explained_tx()
    outs = [t for t in ctx.flagged(alert) if t["direction"] == "out" and t["transaction_id"] not in covered
            and not ctx.is_customer(t.get("counterparty_name"))]
    payees = {util.norm_name(t.get("counterparty_name")) for t in outs}
    if len(payees) < 2:
        return
    m = alert.get("measured") or {}
    first = min(t["booked_at"] for t in outs)
    ins = [t for t in case.transactions if t["direction"] == "in" and in_window(case, t) and t["booked_at"] <= first
           and t.get("channel") in ("faster_payment", "wallet_transfer", "swift") and not ctx.is_customer(t.get("counterparty_name"))
           and t["transaction_id"] in flagged_ids(case) and not re.search(r"salary|wages|payroll", t.get("reference") or "", re.I)]
    tags = chat_tags(case)
    total = sum(t["amount_gbp"] for t in outs)
    text = "{} payments totalling {} went to {} different people in {} ({}), a higher-risk country the customer did not declare".format(
        len(outs), M(total), len(payees), m.get("countries", "a higher-risk country"), join_names([party(t) for t in outs], 4))
    if ins:
        text += ". They were funded by {} received shortly before from {}, none of it documented".format(
            M(sum(t["amount_gbp"] for t in ins)), join_names([party(t) for t in ins], 3))
    ev = []
    chat = tags.get("family_support") or next(iter(case.docs("support_chat")), None)
    if chat:
        text += ". Asked who the payments were for, the customer said only \"{}\" ({}), and nothing on file shows who the recipients are".format(quote(chat, 90), chat.id)
        ev.append(chat.id)
    ctx.add("suspect", "ESC_HIGH_RISK_CORRIDOR", text, ev + [t["transaction_id"] for t in outs[:4] + ins[:3]], rank=82, tag="corridor")


# --------------------------------------------------------------------------------------------------------------
# Requests for information
# --------------------------------------------------------------------------------------------------------------

RULE_ORDER = ["TM22", "TM10", "TM03", "TM05", "TM06", "TM08", "TM04", "TM07", "TM02", "TM01"]


def build_rfi(ctx, unexplained, explains):
    """Nothing is suspicious, but the file does not explain every alert: say exactly what to ask for."""
    case = ctx.case
    who = "the client" if case.is_business else "the customer"
    unexplained = sorted(unexplained, key=lambda pair: RULE_ORDER.index(pair[0]["rule_id"]) if pair[0]["rule_id"] in RULE_ORDER else 99)
    rules = [a["rule_id"] for a, _ in unexplained]
    docs_on_file = [d for d in case.documents if d.type not in SUPPORT_SKIP
                    and not (d.type == "company_register_extract" and util.same_party(d.field("Company") or title_party(d), case.name))]

    if "TM22" in rules:
        alert = next(a for a, _ in unexplained if a["rule_id"] == "TM22")
        m = alert.get("measured") or {}
        entry = ctx.inst.list_entries.get(m.get("list_entry")) or {}
        txs = ctx.flagged(alert)
        subject = m.get("matched_party") or entry.get("name") or "the counterparty"
        first = tx_phrase(txs[0]) if txs else "the counterparty " + subject
        reason = "{} matched internal list entry {} on {} only, and a match on one detail does not prove it is the same party. {} " \
                 "Nothing on file confirms or rules out the match and nothing else in the activity is concerning. Ask {} for details that identify {}: " \
                 "full name, date of birth and address{}, and what the payment was for.".format(
                     first[0].upper() + first[1:], m.get("list_entry"), m.get("matched_on", "one detail"),
                     "The entry is {}{}{}.".format(entry.get("name", subject),
                                                   ", born " + D(util.parse_date(entry["date_of_birth"])) if entry.get("date_of_birth") else "",
                                                   " ({})".format(entry["reason"].rstrip(".")) if entry.get("reason") else ""),
                     who, subject,
                     " (for a company, its registered office, directors and owners)" if re.search(r"\b(ltd|llp|plc|limited)\b", subject, re.I) or entry.get("kind") == "business" else "")
        ev = [m.get("list_entry")] + [t["transaction_id"] for t in txs] + [e for f in explains for e in f.evidence if e not in case.tx_by_id]
        return {"verdict": "RFI", "reason_code": "RFI_COUNTERPARTY_DETAILS", "reason": reason, "evidence": [e for e in dict.fromkeys(ev) if e]}

    if not unexplained and ctx.blocks:
        block = ctx.blocks[0]
        basis = explains[0].text + "." if explains else ""
        reason = "{}. Nothing in the activity is suspicious, so ask {} for {}. {}".format(
            block["fact"], who, block["ask"], basis).strip()
        ev = [block["tx"]["transaction_id"]] + [e for f in explains for e in f.evidence]
        return {"verdict": "RFI", "reason_code": "RFI_PURPOSE_DOCUMENTS", "reason": _trim(reason), "evidence": list(dict.fromkeys(ev))[:10]}

    open_tx = []
    for a, txs in unexplained:
        for t in (txs or ctx.flagged(a)):
            if t not in open_tx:
                open_tx.append(t)
    px = ctx.inst.funding_is_product
    if px:
        # The client's own funding is the product; what needs explaining is what it paid for and any third-party money.
        funded = []
        for t in list(open_tx):
            if t["direction"] == "in" and ctx.is_customer(t.get("counterparty_name")):
                nxt = [o for o in case.transactions if o["direction"] == "out" and o["booked_at"] >= t["booked_at"]
                       and (tx_date(o) - tx_date(t)).days <= 5 and 0.5 * t["amount_gbp"] <= o["amount_gbp"] <= 1.05 * t["amount_gbp"]]
                funded += [o for o in nxt if o not in open_tx and o not in funded]
        big_in = max([t["amount_gbp"] for t in open_tx if t["direction"] == "in"] or [0])
        open_tx = [t for t in open_tx if not (t["direction"] == "in" and ctx.is_customer(t.get("counterparty_name")) and t["amount_gbp"] < 0.5 * big_in)]
        open_tx += funded
        if case.held_payment and case.held_payment not in open_tx:
            open_tx.append(case.held_payment)
    open_tx.sort(key=lambda t: -t["amount_gbp"])
    top = open_tx[0]["amount_gbp"] if open_tx else 0
    big = [t for t in open_tx if t["amount_gbp"] >= 0.1 * top]
    employer = case.customer.get("employer")
    ins = [t for t in big if t["direction"] == "in" and not re.search(r"salary|wages|payroll", t.get("reference") or "", re.I)
           and not (employer and util.same_party(employer, t.get("counterparty_name")))]
    outs = [t for t in big if t["direction"] == "out" and not ctx.is_customer(t.get("counterparty_name"))
            and t.get("channel") not in ("fund_subscription", "card")]
    nothing = "No document on file explains" if not docs_on_file else \
        "The documents on file ({}) do not explain".format(", ".join(d.id for d in docs_on_file[:4]))
    evidence = [t["transaction_id"] for t in (ins[:4] + outs[:4])] or [t["transaction_id"] for t in open_tx[:4]]
    for a, _ in unexplained:
        for t in sorted(ctx.flagged(a), key=lambda t: -t["amount_gbp"])[:6]:
            if t["transaction_id"] not in evidence and len(evidence) < 8:
                evidence.append(t["transaction_id"])
    evidence += [d.id for d in docs_on_file[:4]]

    code, ask = _rfi_ask(ctx, rules, ins, outs, open_tx)
    if "TM04" in rules:
        a4 = next(a for a, _ in unexplained if a["rule_id"] == "TM04")
        f4 = sorted(ctx.flagged(a4), key=lambda t: (t["amount_gbp"], t["transaction_id"]))
        direction = "received from" if (a4.get("measured") or {}).get("direction") == "incoming" else "to"
        described = "the payments {} {} different parties, for example {}".format(
            direction, (a4.get("measured") or {}).get("distinct_counterparties", len(f4)),
            join_names(["{} ({}, {})".format(party(t), M(t["amount_gbp"]), t["transaction_id"]) for t in f4[:3]], 3))
        if outs and direction == "received from":
            described += ", or {}".format(tx_phrase(outs[0]))
        evidence = [t["transaction_id"] for t in f4[:6]] + [t["transaction_id"] for t in outs[:2]] + [d.id for d in docs_on_file[:4]]
    else:
        described = join_names([tx_phrase(t) for t in (ins[:2] + outs[:2])] or [tx_phrase(t) for t in open_tx[:3]], 4)
    bits = [_alert_fact(a, case).rstrip(".") for a, _ in unexplained[:2]]
    facts = "; ".join([bits[0]] + [b[0].lower() + b[1:] if not b.startswith("£") else b for b in bits[1:]]) if bits else ""
    said = _customer_claim(case)
    middle = "{} {}".format(nothing, described)
    if ctx.blocks:
        middle += "; " + ctx.blocks[0]["fact"][0].lower() + ctx.blocks[0]["fact"][1:]
    reason = "{}. {}.{} Nothing in the activity is suspicious yet, so ask {} for {}.".format(
        facts, middle, " " + said if said else "", who, ask)
    return {"verdict": "RFI", "reason_code": code, "reason": _trim(reason), "evidence": list(dict.fromkeys(evidence))}


def _trim(text):
    return re.sub(r"\s+", " ", text).replace("..", ".").strip()


def _customer_claim(case):
    """What the customer has already said about the money, when there is a chat but no document."""
    for chat in case.docs("support_chat"):
        said = " ".join(chat.customer_turns())
        m = re.search(r"[^.]*\b(sell|selling|sold|sale)\b[^.]*\.", said)
        if m:
            return "In {} the customer says \"{}\" but no receipt or other record of that sale is on file.".format(chat.id, m.group(0).strip().rstrip("."))
    return ""


def _alert_fact(alert, case):
    m = alert.get("measured") or {}
    rule = alert["rule_id"]
    if rule == "TM01":
        return "The account received {} in 30 days, {:g} times the declared monthly figure of {} (limit {:g} times).".format(
            M(m.get("received_gbp", 0)), m.get("multiple", 0), M(m.get("baseline_gbp", 0)), m.get("limit_multiple", 0))
    if rule == "TM02":
        return "{} came in and {} ({:.0%}) went out again within the rule's window.".format(
            M(m.get("received_gbp", 0)), M(m.get("paid_out_gbp", 0)), m.get("share", 0))
    if rule == "TM07":
        return "The account was dormant for {} days and then {} moved.".format(m.get("dormant_days", "over 180"), M(m.get("moved_gbp", 0)))
    if rule == "TM08":
        return "{} moved to or from an account not in {}'s name.".format(M(m.get("total_gbp", 0)), case.name)
    if rule == "TM10":
        return "Units in {} bought for {} were sold back after {} days for {}.".format(
            m.get("fund", "a fund"), M(m.get("bought_gbp", 0)), m.get("held_days", "under 90"), M(m.get("redeemed_gbp", 0)))
    if rule == "TM06":
        return "{} moved to or from {}, a higher-risk country the customer did not declare.".format(M(m.get("total_gbp", 0)), m.get("countries", "a higher-risk country"))
    if rule == "TM04":
        return "{} different parties {} in 30 days (limit {}), {} in total.".format(
            m.get("distinct_counterparties", "Many"), "paid the account" if m.get("direction") == "incoming" else "were paid by the account",
            m.get("limit", ""), M(m.get("total_gbp", 0)))
    if rule == "TM03":
        return "{} transactions just under the threshold totalled {} within 10 days.".format(m.get("count") or m.get("payments") or "Several", M(m.get("total_gbp", 0)))
    if rule == "TM05":
        return "A single cash transaction of {} met the limit of {}.".format(M(m.get("amount_gbp", 0)), M(m.get("limit_gbp", 0)))
    return alert.get("message", "")


def _rfi_ask(ctx, rules, ins, outs, open_tx):
    """(reason code, the specific document to ask for), chosen from what the unexplained transactions look like."""
    case = ctx.case
    px = ctx.inst.funding_is_product
    t_in = ins[0] if ins else None
    t_out = outs[0] if outs else None
    third_in = [t for t in ins if not ctx.is_customer(t.get("counterparty_name"))]

    if "TM10" in rules:
        return "RFI_PURPOSE_DOCUMENTS", "the reason for selling the fund units so soon after buying them, with a document that supports it " \
               "(for example a letter showing a change in employment or another need for the money)"
    if px:
        if "TM03" in rules:
            a = case.alert("TM03")
            who = (a.get("measured") or {}).get("beneficiary") or (party(t_out) if t_out else "the beneficiary")
            return "RFI_PURPOSE_DOCUMENTS", "the contract or payment schedule with {} showing what each of the {} payments was for".format(
                who, (a.get("measured") or {}).get("payments", "repeated"))
        if "TM04" in rules:
            m = (case.alert("TM04").get("measured") or {})
            return "RFI_PURPOSE_DOCUMENTS", "the payment file or contracts identifying the {} payees and what each was paid for".format(
                m.get("distinct_counterparties", "many"))
        if "TM07" in rules:
            return "RFI_PURPOSE_DOCUMENTS", "confirmation of why trading stopped and has resumed, with {}".format(
                _doc_for_outflow(ctx, t_out) if t_out else "the invoice for the first payment since the account was reactivated")
        if third_in and not outs:
            return "RFI_SOURCE_OF_FUNDS", _doc_for_inflow(ctx, third_in[0])
        if third_in and outs:
            return "RFI_PURPOSE_DOCUMENTS", "the invoices the client raised to {} for these receipts, and {}".format(
                join_names([party(t) for t in third_in], 3), _doc_for_outflow(ctx, t_out))
        if t_out is not None:
            return "RFI_PURPOSE_DOCUMENTS", "{}, and the customer order it relates to if the purchase is larger than usual".format(_doc_for_outflow(ctx, t_out))
        return "RFI_PURPOSE_DOCUMENTS", "documents showing what the funding was for, such as the supplier invoice or customer order behind it"
    # A trading customer's unexplained takings call for the records behind them, not a single receipt.
    cash_in = [t for t in ins if (t.get("channel") or "").startswith("cash")]
    trade_refs = [t for t in ins if re.search(r"takings|sales|trading|\btill\b", t.get("reference") or "", re.I)]
    trader = case.is_business or bool(re.search(r"trader|stall|self[- ]employed|shopkeeper|market", case.customer.get("occupation") or "", re.I))
    if (cash_in or trade_refs) and (trader or trade_refs):
        days = sorted({D(tx_date(t)) for t in (cash_in or trade_refs)}, key=lambda x: util.parse_date(x))
        return "RFI_BUSINESS_RECORDS", "the business records behind the cash paid in: the takings book, till or collection reports and cash " \
               "reconciliation for {}, showing each amount banked ({})".format(
                   days[0] if len(days) == 1 else "{} to {}".format(days[0], days[-1]),
                   join_names(["{} {}".format(M(t["amount_gbp"]), t["transaction_id"]) for t in (cash_in or trade_refs)[:3]], 3))
    if case.is_business and ins and not outs:
        return "RFI_BUSINESS_RECORDS", "the business records behind these receipts, such as management accounts, the sales ledger or the invoices " \
               "raised to {}".format(join_names([party(t) for t in ins[:3]], 3))
    if outs:
        payers = {util.norm_name(t.get("counterparty_name")) for t in ins}
        if ins and len(payers) <= 2 and "TM04" not in rules:
            return "RFI_PURPOSE_DOCUMENTS", "documents showing what the money was for: {} and {}".format(
                _doc_for_inflow(ctx, t_in), _doc_for_outflow(ctx, t_out))
        return "RFI_PURPOSE_DOCUMENTS", _doc_for_outflow(ctx, t_out)
    if t_in is not None:
        return "RFI_SOURCE_OF_FUNDS", _doc_for_inflow(ctx, t_in)
    if open_tx:
        return "RFI_PURPOSE_DOCUMENTS", "documents showing what {} was for".format(join_names([tx_phrase(t) for t in open_tx[:3]], 3))
    return "RFI_PURPOSE_DOCUMENTS", "documents showing what the alerted activity was for"


def _doc_for_inflow(ctx, tx):
    case = ctx.case
    ref = (tx.get("reference") or "").upper()
    name = re.sub(r"\s+client account$", "", tx.get("counterparty_name") or "the payer", flags=re.I)
    if (tx.get("channel") or "").startswith("cash"):
        return "a receipt or other record showing where the {} in cash came from (who paid it, when and for what)".format(M(tx["amount_gbp"]))
    if ctx.is_customer(name):
        return "a statement for the sending account showing {} holds it".format("the client" if case.is_business else "the customer")
    surname = util.surname(case.name)
    if re.search(r"&|\band\b", name) and surname and surname in util.norm_name(name):
        return "a statement for the joint account \"{}\" showing the customer is one of its holders".format(name)
    if "GIFT" in ref:
        return "a signed gift letter from {} giving their relationship to the customer, date of birth and the source of the money".format(name)
    if re.search(r"SETTLEMENT", ref):
        return "the settlement agreement letter from {} showing the amount and what it was paid for".format(name)
    if re.search(r"ESTATE|INHERIT|BEQUEST|LEGACY", ref):
        return "the solicitor's letter from {} confirming the inheritance and its amount".format(name)
    if re.search(r"SALE|COMPLETION|PROCEEDS", ref) or re.search(r"conveyanc|solicitor|legal|law\b", name, re.I):
        return "the completion statement or solicitor's letter from {} showing what the money is".format(name)
    if re.search(r"CLAIM|INSUR|POLICY", ref) or re.search(r"insur|assurance", name, re.I):
        return "the claim settlement letter from {}".format(name)
    if re.search(r"LOAN", ref):
        return "the loan agreement with {}".format(name)
    if re.search(r"DEPOSIT", ref):
        return "the letter from {} confirming the deposit return".format(name)
    if re.search(r"ADVANCE|FACTOR|FINANCE", ref) or re.search(r"finance|factor", name, re.I):
        return "the invoice finance agreement with {} and the remittance advice for this advance".format(name)
    return "a document from {} showing what the payment (reference \"{}\") was for and where the money came from".format(name, tx.get("reference") or "none")


def _doc_for_outflow(ctx, tx):
    ref = (tx.get("reference") or "")
    name = tx.get("counterparty_name") or "the payee"
    if re.search(r"motor|car|auto|vehicle", name, re.I):
        return "the dealer's invoice from {}".format(name)
    if re.search(r"travel|holiday|tours?\b", name, re.I) or re.search(r"\bBK\d+|BOOKING", ref):
        return "the booking confirmation from {} listing the travellers and the price".format(name)
    if re.search(r"homes|lettings|estates|property|rent", name, re.I) or re.search(r"DEPOSIT|RENT", ref.upper()):
        return "the receipt or tenancy paperwork from {}".format(name)
    return "the invoice or contract from {} (reference \"{}\")".format(name, ref or "none")


# --------------------------------------------------------------------------------------------------------------
# Running order
# --------------------------------------------------------------------------------------------------------------

# Explanations first (what the documents on file account for), then signs of crime.
CHECKS = [("explain", check) for check in (
    own_account, event_documents, invoices, invoice_tables, group_company, cash_records, cash_sale, cash_redeposit, refunds,
    group_booking, cancellation_refund, early_redemption_reason, own_funding, returning_customer, list_match,
    updated_profile, held_payment_needs_invoice)] + [("suspect", check) for check in (
        fraud_reports, chat_admissions, shared_device, staff_notes, dissolved_payer, linked_party_pass_through, cash_moved_on,
        corridor_fan_out, third_party_payout, third_party_corridor_funding, account_control)]


def run_all(ctx):
    for _, check in CHECKS:
        check(ctx)
