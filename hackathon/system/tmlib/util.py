"""Small parsing and formatting helpers. Standard library only."""
import datetime
import re

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"])}
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]

_MONEY = re.compile(r"(?:£|GBP\s?)(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_LONG_DATE = re.compile(r"\b(\d{1,2})\s+(%s)\s+(\d{4})\b" % "|".join(MONTH_NAMES), re.I)
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTH_YEAR = re.compile(r"\b(%s)\s+(\d{4})\b" % "|".join(MONTH_NAMES), re.I)
_COMPANY_SUFFIX = re.compile(
    r"\b(ltd|limited|llp|plc|inc|co|company|sarl|gmbh|bv|sa|srl|spa|llc|pte|pvt|sp z o o|ag|oy|ab|as|nv)\b\.?", re.I)


def money_values(text):
    """Every pound amount written in the text, in order, as floats."""
    return [float(m.replace(",", "")) for m in _MONEY.findall(text or "")]


def parse_money(text):
    """The first pound amount in the text, or None."""
    vals = money_values(text)
    return vals[0] if vals else None


def fmt_money(amount):
    return "£{:,.2f}".format(amount)


def same_amount(a, b, tolerance=0.005):
    return a is not None and b is not None and abs(a - b) <= tolerance


def parse_date(text):
    """A date from '15 September 2026', '2026-09-15' or '2026-09-15T11:02:00Z'. None when there is none."""
    if not text:
        return None
    m = _ISO_DATE.search(text)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _LONG_DATE.search(text)
    if m:
        try:
            return datetime.date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    return None


def find_dates(text):
    """Every full date written in the text, in order, with where it starts: [(date, position)]."""
    found = []
    for m in re.finditer(r"%s|%s" % (_LONG_DATE.pattern, _ISO_DATE.pattern), text or "", re.I):
        d = parse_date(m.group(0))
        if d:
            found.append((d, m.start()))
    return found


def all_dates(text):
    """Every full date written in the text, in order."""
    return [d for d, _ in find_dates(text)]


def stated_births(text):
    """Dates the text itself labels as a date of birth ('born 20 August 1966', 'DOB: 8 June 1969')."""
    out = []
    for d, pos in find_dates(text):
        lead = (text or "")[max(0, pos - 30):pos]
        if re.search(r"\b(born|birth|d\.?o\.?b\.?)\b[^.;]*$", lead, re.I):
            out.append(d)
    return out


def same_address(a, b):
    """True when two addresses are the same once case and punctuation are ignored, or one contains the other."""
    na, nb = norm_name(a), norm_name(b)
    return bool(na) and bool(nb) and (na == nb or na in nb or nb in na)


def parse_month_year(text):
    """(year, month) from 'August 1978' or from a full date. None when there is none."""
    d = parse_date(text)
    if d:
        return (d.year, d.month)
    m = _MONTH_YEAR.search(text or "")
    if m:
        return (int(m.group(2)), MONTHS[m.group(1).lower()])
    return None


def parse_datetime(text):
    """A timezone-naive datetime from an ISO timestamp such as '2026-09-15T11:02:00Z'."""
    return datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")


def fmt_date(d):
    return "{} {} {}".format(d.day, MONTH_NAMES[d.month - 1], d.year)


def norm_name(name):
    """Lower-case, punctuation-free form of a person or company name, for comparison."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def norm_company(name):
    """As norm_name, with the legal suffix removed, so 'Elmcote Consulting Ltd' equals 'Elmcote Consulting'."""
    s = _COMPANY_SUFFIX.sub(" ", norm_name(name))
    return re.sub(r"\s+", " ", s).strip()


def same_party(a, b):
    """True when two names are the same person or company, ignoring case, punctuation and legal suffix."""
    if not a or not b:
        return False
    return norm_name(a) == norm_name(b) or (norm_company(a) != "" and norm_company(a) == norm_company(b))


def party_like(a, b):
    """True when one name is the other plus a qualifier, as in 'Talby Conveyancing LLP client account'."""
    if same_party(a, b):
        return True
    ca, cb = norm_company(a), norm_company(b)
    if len(ca) < 5 or len(cb) < 5:
        return False
    return (" " + ca + " ").startswith(" " + cb + " ") or (" " + cb + " ").startswith(" " + ca + " ")


def mentions(text, name):
    """True when the text names the party (company suffix optional)."""
    if not text or not name:
        return False
    t = " " + norm_name(text) + " "
    n = norm_name(name)
    c = norm_company(name)
    return (" " + n + " ") in t or (len(c) >= 5 and (" " + c + " ") in t)


def surname(name):
    parts = norm_name(name).split()
    return parts[-1] if parts else ""


def account_tail(account):
    """The last four digits of an account reference such as '77-75-31 69787325' or 'account ending 7144'."""
    digits = re.sub(r"\D", "", account or "")
    return digits[-4:] if len(digits) >= 4 else None


def same_account(a, b):
    """True when two account references are the same, comparing full digits or, failing that, the last four."""
    da, db = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    if not da or not db:
        return False
    if len(da) >= 8 and len(db) >= 8:
        return da == db or da.endswith(db) or db.endswith(da)
    return da[-4:] == db[-4:]
