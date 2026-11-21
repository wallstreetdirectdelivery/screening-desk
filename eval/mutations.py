#!/usr/bin/env python3
"""Mutation tests: change one detail of a real development case and check the verdict moves the safe way.

Usage: python3 eval/mutations.py [<folder holding the institution folders>]

The development data has no awkward variants (a document dated after the payment, a payment to the wrong
account, a letter that only looks like it gives a birth date). The unseen cases might, so each test builds
one in memory and asserts the system does not close it, or does not escalate a case that should still close.
Development aid only; the system never reads this.
"""
import copy
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "hackathon", "system"))
from tmlib import data, detect, engine  # noqa: E402

DATA = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "hackathon", "institutions")
_cache = {}


def inst_for(name):
    if name not in _cache:
        _cache[name] = data.Institution(os.path.join(DATA, name))
    return _cache[name]


def load(name, case_id):
    inst = inst_for(name)
    row = next(r for r in inst.case_rows if r["case_id"] == case_id)
    return inst, copy.deepcopy(inst.build_case(row))


def decide(inst, case):
    ctx = engine.Context(inst, case)
    detect.run_all(ctx)
    return engine.decide(ctx, detect.build_rfi)


def rewrite(case, doc_type, change, dated=None):
    """Replace the text (and optionally the date) of the first document of a type."""
    for i, d in enumerate(case.documents):
        if d.type == doc_type:
            meta = {"doc_id": d.id, "type": d.type, "title": d.title, "dated": dated or (d.date.isoformat() if d.date else None)}
            case.documents[i] = data.Document(meta, change(d.raw))
            return
    raise AssertionError("no {} document in {}".format(doc_type, case.id))


results = []


def check(name, verdict, allowed, base=None):
    ok = verdict in allowed
    results.append(ok)
    print("{}  {:68s} got {:8s} (was {}; allowed: {})".format("PASS" if ok else "FAIL", name, verdict, base or "-", "/".join(allowed)))


def check_code(name, result, verdict, code):
    ok = result["verdict"] == verdict and result["reason_code"] == code
    results.append(ok)
    print("{}  {:68s} got {}/{} (expected {}/{})".format("PASS" if ok else "FAIL", name, result["verdict"], result["reason_code"], verdict, code))


def add_doc(case, doc_id, doc_type, title, dated, body):
    case.documents.append(data.Document({"doc_id": doc_id, "type": doc_type, "title": title, "dated": dated}, "# {}\n\n{}\n".format(title, body)))


def drop_docs(case, *types):
    case.documents = [d for d in case.documents if d.type not in types]


DEVICE = "| Device | First seen | Customers who logged in from it | Login countries |\n|---|---|---|---|\n| Z9-0001 | 1 August 2026 | {}, XX-C1, XX-C2 | United Kingdom, Romania |"


# ---- scenarios that are not in the development data but could be in the unseen cases

def test_new_business_takings_without_records():
    inst, case = load("bank", "BK-CS208841")
    drop_docs(case, "cash_collection_reports", "cash_handling_procedure")
    check_code("business cash takings with no records on file", decide(inst, case), "RFI", "RFI_BUSINESS_RECORDS")
    inst, case = load("emoney", "EM-CS236536")
    drop_docs(case, "takings_book")
    check_code("market trader's cash top-ups with no takings book", decide(inst, case), "RFI", "RFI_BUSINESS_RECORDS")
    inst, case = load("bank", "BK-CS109012")
    check_code("individual's undocumented cash stays a source-of-funds request", decide(inst, case), "RFI", "RFI_SOURCE_OF_FUNDS")


def test_new_someone_else_controls_account():
    inst, case = load("bank", "BK-CS248040")
    add_doc(case, "DEV-0001", "device_export", "Device and login export", "2026-09-30", DEVICE.format(case.customer["customer_id"]))
    check_code("unexplained money on an account whose device is shared", decide(inst, case), "ESCALATE", "ESC_ACCOUNT_MISUSE")
    inst, case = load("bank", "BK-CS248040")
    add_doc(case, "CHT-0001", "support_chat", "Chat with the customer", "2026-09-30",
            "**Agent:** Can you tell us about this payment?\n\n**Customer:** My boyfriend uses my account, he set it all up and I don't really look at it.")
    check_code("unexplained money and the customer says someone else uses it", decide(inst, case), "ESCALATE", "ESC_ACCOUNT_MISUSE")
    inst, case = load("bank", "BK-CS245491")
    add_doc(case, "DEV-0001", "device_export", "Device and login export", "2026-09-30", DEVICE.format(case.customer["customer_id"]))
    check_code("shared device on a fully documented case is context only", decide(inst, case), "CLOSE", "DOCUMENTED_EVENT")


def test_new_updated_profile_on_file():
    inst, case = load("investment", "IV-CS696112")
    add_doc(case, "KYC-0001", "profile_update", "Updated customer profile", "2026-08-01", "**Updated expected monthly income:** £20,000.00")
    check_code("own-account transfers over 5 times, updated profile on file", decide(inst, case), "CLOSE", "WITHIN_PROFILE")
    inst, case = load("investment", "IV-CS696112")
    add_doc(case, "KYC-0001", "profile_update", "Updated customer profile", "2026-08-01", "**Updated expected monthly income:** £8,000.00")
    check_code("updated profile on file that is still too low", decide(inst, case), "RFI", "RFI_UPDATED_PROFILE")
    inst, case = load("bank", "BK-CS248040")
    add_doc(case, "KYC-0001", "profile_update", "Updated customer profile", "2026-08-01", "**Updated expected monthly income:** £90,000.00")
    check_code("a higher declared income does not explain undocumented money", decide(inst, case), "RFI", "RFI_SOURCE_OF_FUNDS")


def test_staff_note_negated_all_clear():
    inst, case = load("payments_fx", "PX-CS102578")
    base = decide(inst, case)["verdict"]
    rewrite(case, "staff_note", lambda t: t + "\nThe director was not satisfied with the explanation.\n")
    check("staff note containing 'not satisfied with the explanation'", decide(inst, case)["verdict"], ["ESCALATE"], base)


def test_invoice_paid_to_other_account():
    inst, case = load("payments_fx", "PX-CS118701")
    base = decide(inst, case)["verdict"]
    case.held_payment["counterparty_account"] = "11-11-11 11111111"
    check("payment went to a different account from the invoice", decide(inst, case)["verdict"], ["ESCALATE", "RFI"], base)


def test_second_unexplained_funding():
    inst, case = load("payments_fx", "PX-CS101658")
    base = decide(inst, case)["verdict"]
    alert = case.alert("TM01")
    big = max((t for t in case.alert_transactions(alert)), key=lambda t: t["amount_gbp"])
    twin = dict(big, transaction_id=big["transaction_id"] + "X")
    case.transactions.append(twin)
    case.tx_by_id[twin["transaction_id"]] = twin
    alert["transaction_ids"].append(twin["transaction_id"])
    alert["measured"]["received_gbp"] += twin["amount_gbp"]
    alert["measured"]["multiple"] = round(alert["measured"]["received_gbp"] / alert["measured"]["baseline_gbp"], 2)
    check("a second funding receipt with no payment behind it", decide(inst, case)["verdict"], ["RFI", "ESCALATE"], base)


def test_list_address_identical():
    inst, case = load("bank", "BK-CS144992")
    base = decide(inst, case)["verdict"]
    entry = inst.list_entries[case.alert("TM22")["measured"]["list_entry"]]
    rewrite(case, "company_register_extract",
            lambda t: "\n".join("**Registered office:** " + entry["address"] if line.startswith("**Registered office:**") else line for line in t.split("\n")))
    check("register shows the same address as the list entry", decide(inst, case)["verdict"], ["RFI", "ESCALATE"], base)


def test_letter_with_unrelated_old_date():
    inst, case = load("investment", "IV-CS106722")
    base = decide(inst, case)["verdict"]
    import re
    rewrite(case, "gift_letter", lambda t: re.sub(r"\(?(born|DOB:?|date of birth:?)\s+(on\s+)?\d{1,2} [A-Z][a-z]+ \d{4}\)?", "a customer of my bank since 3 March 1998", t, flags=re.I))
    check("gift letter with an old date that is not a birth date", decide(inst, case)["verdict"], ["RFI", "ESCALATE"], base)


def test_everyday_phrase_in_chat():
    inst, case = load("bank", "BK-CS150683")
    base = decide(inst, case)["verdict"]
    rewrite(case, "support_chat", lambda t: t + "\n\n**Customer:** I am paying the hospital on their behalf and will send it on today.\n")
    check("harmless chat phrase on a documented hospital payment", decide(inst, case)["verdict"], ["CLOSE"], base)


def test_receipt_equals_gross_not_net():
    inst, case = load("investment", "IV-CS100692")
    base = decide(inst, case)["verdict"]
    doc = case.docs("completion_statement")[0]
    gross = float(doc.field("Sale price").replace("£", "").replace(",", ""))
    alert = case.alert("TM08")
    tx = case.alert_transactions(alert)[0]
    tx["amount_gbp"] = tx["amount"] = gross
    check("receipt equals the sale price, not the amount paid to the client", decide(inst, case)["verdict"], ["RFI", "ESCALATE"], base)


def test_schedule_dated_after_payments():
    inst, case = load("payments_fx", "PX-CS121073")
    base = decide(inst, case)["verdict"]
    rewrite(case, "contract_schedule", lambda t: t, dated="2026-12-01")
    check("payment schedule dated after the payments", decide(inst, case)["verdict"], ["RFI", "ESCALATE"], base)


def test_letter_dated_long_after_money():
    inst, case = load("emoney", "EM-CS104023")
    base = decide(inst, case)["verdict"]
    rewrite(case, "settlement_letter", lambda t: t, dated="2027-06-01")
    check("settlement letter dated months after the money arrived", decide(inst, case)["verdict"], ["RFI", "ESCALATE"], base)


def test_duplicate_invoice_lines():
    inst, case = load("payments_fx", "PX-CS159922")
    base = decide(inst, case)["verdict"]
    doc = case.docs("freight_invoices")[0]
    row = doc.tables[0][0]
    first = next(t for t in case.transactions if t["direction"] == "in" and t["counterparty_name"] == row["Exporter"])
    twin = dict(first, transaction_id=first["transaction_id"] + "X")
    case.transactions.append(twin)
    case.tx_by_id[twin["transaction_id"]] = twin
    for a in case.alerts:
        if first["transaction_id"] in a["transaction_ids"]:
            a["transaction_ids"].append(twin["transaction_id"])
    line = "| {} | {} | {} |".format(row["Invoice"] + "B", row["Exporter"], row["Amount"])
    rewrite(case, "freight_invoices", lambda t: t.rstrip() + "\n" + line + "\n")
    check("two invoices of the same value to one payer, both paid", decide(inst, case)["verdict"], ["CLOSE"], base)


def test_runner_survives_bad_file_and_missing_alerts():
    src = os.path.join(DATA, "bank")
    tmp = tempfile.mkdtemp(prefix="tm-mut-")
    try:
        folder = os.path.join(tmp, "bank")
        os.makedirs(os.path.join(folder, "evidence"))
        inst = inst_for("bank")
        rows = inst.case_rows[:3]
        customers = {r["customer_id"] for r in rows}
        import json
        keep_alerts = set(rows[0]["alert_ids"]) | set(rows[1]["alert_ids"])   # the third case loses its alerts
        for name in ("cases", "alerts", "customers", "transactions", "evidence", "previous_reviews", "internal_list"):
            with open(os.path.join(src, name + ".jsonl"), encoding="utf-8") as fh, open(os.path.join(folder, name + ".jsonl"), "w", encoding="utf-8") as out:
                for line in fh:
                    r = json.loads(line)
                    if name == "cases" and r not in rows:
                        continue
                    if name == "alerts" and r["alert_id"] not in keep_alerts:
                        continue
                    if name in ("customers", "transactions", "evidence", "previous_reviews") and r.get("customer_id") not in customers:
                        continue
                    out.write(line)
                    if name == "evidence":
                        shutil.copy(os.path.join(src, r["file"]), os.path.join(folder, r["file"]))
                if name == "transactions":
                    out.write("{this line is not json\n")
        shutil.copy(os.path.join(src, "POLICY.md"), folder)
        first_doc = sorted(os.listdir(os.path.join(folder, "evidence")))[0]
        with open(os.path.join(folder, "evidence", first_doc), "ab") as fh:
            fh.write(b"\xe9\xff stray bytes")
        out_path = os.path.join(tmp, "out.jsonl")
        proc = subprocess.run([os.path.join(ROOT, "hackathon", "system", "run"), folder, out_path], capture_output=True, text=True)
        lines = [json.loads(x) for x in open(out_path, encoding="utf-8")] if os.path.exists(out_path) else []
        ok = proc.returncode == 0 and len(lines) == 3
        results.append(ok)
        print("{}  {:68s} exit {} with {} of 3 lines".format("PASS" if ok else "FAIL", "runner with stray bytes in a document and a bad JSON line", proc.returncode, len(lines)))
        third = next((x for x in lines if x["case_id"] == rows[2]["case_id"]), {})
        check("case whose alert rows are missing", third.get("verdict", "none"), ["ESCALATE"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn_name in sorted(k for k in dict(globals()) if k.startswith("test_")):
        try:
            globals()[fn_name]()
        except Exception as exc:
            results.append(False)
            print("FAIL  {} raised {}: {}".format(fn_name, type(exc).__name__, exc))
    print("\n{} of {} checks passed".format(sum(results), len(results)))
    sys.exit(0 if all(results) else 1)
