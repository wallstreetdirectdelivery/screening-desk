"""Load one institution folder and give each case everything on file for it. Standard library only."""
import collections
import json
import os
import re

from . import util

BASELINE_TYPES = ("due_diligence_summary", "proof_of_address")


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue   # one unreadable line must not stop the run
            if isinstance(row, dict):
                rows.append(row)
    return rows


class Document:
    """One Markdown document on file, parsed into labelled fields, tables and free text."""

    def __init__(self, meta, text):
        self.id = meta["doc_id"]
        self.type = meta.get("type") or ""
        self.title = meta.get("title") or ""
        self.date = util.parse_date(meta.get("dated"))
        self.raw = text
        self.fields = collections.OrderedDict()
        self.tables = []   # each table is a list of {column: cell} rows
        self.prose = []    # free-text lines: anything that is not a heading, a labelled field or a table row
        self.turns = []    # for chats: (speaker, text)
        self._parse(text)
        self.body = "\n".join(
            ["{}: {}".format(k, v) for k, v in self.fields.items()] +
            [" | ".join(str(v) for v in row.values()) for table in self.tables for row in table] +
            self.prose + [t for _, t in self.turns])
        self.text = self.title + "\n" + self.body

    def _parse(self, text):
        header = None
        table = None
        for line in text.split("\n"):
            s = line.strip()
            if not s:
                header, table = None, None
                continue
            if s.startswith("# "):
                if not self.title:
                    self.title = s[2:].strip()
                continue
            if s.startswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if header is None:
                    header = cells
                    table = []
                    self.tables.append(table)
                elif all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    continue
                else:
                    table.append(collections.OrderedDict(zip(header, cells)))
                continue
            header, table = None, None
            m = re.match(r"\*\*([^*]+?):\*\*\s*(.*)$", s)
            if m:
                key, value = m.group(1).strip(), m.group(2).strip()
                if key in ("Document ID", "Dated"):
                    continue
                if self.type == "support_chat" and key in ("Agent", "Customer", "Client"):
                    self.turns.append((key, value))
                    continue
                self.fields[key] = value
                continue
            self.prose.append(s)

    def field(self, *names):
        """The first labelled field whose label matches one of the names, ignoring case."""
        lowered = {k.lower(): v for k, v in self.fields.items()}
        for n in names:
            if n.lower() in lowered:
                return lowered[n.lower()]
        return None

    def amounts(self):
        return util.money_values(self.body)

    def customer_turns(self):
        return [t for who, t in self.turns if who.lower() in ("customer", "client")]

    def mentions(self, name):
        return util.mentions(self.text, name)


class Case:
    def __init__(self, row, customer, alerts, transactions, documents, reviews):
        self.id = row["case_id"]
        self.row = row
        self.customer = customer
        self.alerts = sorted(alerts, key=lambda a: (a.get("rule_id", ""), a.get("alert_id", "")))
        self.alert_ids = list(row.get("alert_ids") or [a["alert_id"] for a in self.alerts])
        self.transactions = sorted(transactions, key=lambda t: (t["booked_at"], t["transaction_id"]))
        self.tx_by_id = {t["transaction_id"]: t for t in self.transactions}
        self.documents = sorted(documents, key=lambda d: d.id)
        self.reviews = reviews
        window = row.get("review_window") or {}
        self.window_from = util.parse_date(window.get("from"))
        self.window_to = util.parse_date(window.get("to"))
        self.held_payment = self.tx_by_id.get(row.get("held_payment_id")) if row.get("held_payment_id") else None

    @property
    def rules(self):
        return sorted({a["rule_id"] for a in self.alerts})

    def alert(self, rule_id):
        for a in self.alerts:
            if a["rule_id"] == rule_id:
                return a
        return None

    def alert_transactions(self, alert):
        return [self.tx_by_id[t] for t in alert.get("transaction_ids", []) if t in self.tx_by_id]

    def docs(self, *types):
        return [d for d in self.documents if d.type in types]

    def supporting_docs(self):
        """Documents other than the two onboarding ones."""
        return [d for d in self.documents if d.type not in BASELINE_TYPES]

    @property
    def is_business(self):
        return self.customer.get("kind") == "business"

    @property
    def name(self):
        return self.customer.get("name") or ""

    @property
    def declared(self):
        return self.customer.get("declared") or {}


class Institution:
    def __init__(self, folder):
        self.folder = folder
        self.name = os.path.basename(os.path.normpath(folder))
        policy_path = os.path.join(folder, "POLICY.md")
        self.policy = open(policy_path, encoding="utf-8").read() if os.path.exists(policy_path) else ""
        self.case_rows = [r for r in load_jsonl(os.path.join(folder, "cases.jsonl")) if r.get("case_id")]
        self.customers = {c["customer_id"]: c for c in load_jsonl(os.path.join(folder, "customers.jsonl")) if c.get("customer_id")}
        self.list_entries = {e["entry_id"]: e for e in load_jsonl(os.path.join(folder, "internal_list.jsonl")) if e.get("entry_id")}
        self._alerts = collections.defaultdict(list)
        alert_by_id = {}
        for a in load_jsonl(os.path.join(folder, "alerts.jsonl")):
            if not a.get("alert_id") or not a.get("rule_id"):
                continue
            self._alerts[a.get("case_id")].append(a)
            alert_by_id[a["alert_id"]] = a
        self._alert_by_id = alert_by_id
        wanted = {r.get("customer_id") for r in self.case_rows}
        self._tx = collections.defaultdict(list)
        for t in load_jsonl(os.path.join(folder, "transactions.jsonl")):
            if t.get("customer_id") in wanted and t.get("transaction_id") and t.get("booked_at"):
                self._tx[t["customer_id"]].append(t)
        self._evidence = collections.defaultdict(list)
        for e in load_jsonl(os.path.join(folder, "evidence.jsonl")):
            if e.get("doc_id"):
                self._evidence[e.get("customer_id")].append(e)
        self._reviews = collections.defaultdict(list)
        for r in load_jsonl(os.path.join(folder, "previous_reviews.jsonl")):
            self._reviews[r.get("customer_id")].append(r)
        self.higher_risk_countries = self._policy_countries()
        # Settings read from the institution's own policy text, so the system follows whichever SOP it is given.
        flat = " ".join(self.policy.split())
        self.tm02_third_party_only = bool(re.search(r"TM02[^|]*\|[^|]*\|[^|]*other than the client", flat))
        self.funding_is_product = bool(re.search(r"fund their account from their own bank", flat))
        m = re.search(r"Payments of £([\d,]+(?:\.\d+)?) or more to anyone other than the client need an invoice", flat)
        self.invoice_threshold = float(m.group(1).replace(",", "")) if m else None
        m = re.search(r"at (\d+(?:\.\d+)?) times the declared", flat)
        self.own_account_multiple = float(m.group(1)) if m else 5.0

    def _policy_countries(self):
        m = re.search(r"Higher-risk countries[^:\n]*:\s*([^\n]+)", self.policy)
        if not m:
            return set()
        return {c.strip().rstrip(".") for c in m.group(1).split(",") if c.strip()}

    def _document(self, meta):
        path = os.path.join(self.folder, meta.get("file") or os.path.join("evidence", meta["doc_id"] + ".md"))
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except (OSError, ValueError):
            text = ""
        return Document(meta, text)

    def build_case(self, row):
        cid = row.get("customer_id")
        alerts = [self._alert_by_id[a] for a in row.get("alert_ids") or [] if a in self._alert_by_id]
        if not alerts:
            alerts = self._alerts.get(row["case_id"], [])
        return Case(row, self.customers.get(cid, {}), alerts, self._tx.get(cid, []),
                    [self._document(m) for m in self._evidence.get(cid, [])], self._reviews.get(cid, []))

    def cases(self):
        for row in self.case_rows:
            yield self.build_case(row)
