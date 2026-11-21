#!/usr/bin/env python3
"""AI supervisor: a second-line review of escalated cases by a language model.

The rule engine decides every case and its output never changes. For a case it has escalated, this module
asks a model to act as the MLRO's deputy: test whether the grounds hold up, list what the file does not rule
out, suggest further checks that do not involve the customer, and draft the report narrative. The model
cannot change the verdict; where it thinks the grounds are weak it says so for a person to look at.

Set a model key in the environment or in ui/.env (which git ignores); llm.py lists the providers:

    GLM_API_KEY=...   or   OPENROUTER_API_KEY=...   or   XAI_API_KEY=...

Review every escalation at one institution and print a summary (results are cached for the viewer):

    python3 ui/supervisor.py bank [--limit 20] [--workers 6]

Standard library only. This is part of the viewer, not of the submitted system.
"""
import concurrent.futures
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import llm  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".cache", "supervisor")
PROMPT_VERSION = "1"

SYSTEM = """You are the second-line reviewer at a UK regulated financial institution: the deputy to the Money Laundering Reporting Officer (MLRO).

A first-line screening engine has escalated a transaction monitoring case to the MLRO. You are given the customer, the alerts, the transactions the alerts fired on, the documents on file, the engine's findings and its reason.

Your job is to review the escalation, not to decide the case. You cannot close it or change the verdict. Work only from the facts given. Never invent a fact, a document or an amount. Cite document IDs and transaction IDs exactly as written.

Remember the tipping-off rule: no check you suggest may involve contacting or questioning the customer.

Reply with one JSON object and nothing else, with these keys:
- "assessment": "supports_escalation" if the facts on file support a suspicion, or "weak_grounds" if they do not hold up or an innocent explanation on file fits better.
- "confidence": "high", "medium" or "low".
- "summary": two sentences, plain English, for the MLRO.
- "strongest_facts": up to four short strings, each one fact with the ID it comes from.
- "gaps": up to three short strings: what the file does not show, or an innocent explanation it does not rule out. Use an empty list if there are none.
- "further_checks": up to three short strings: internal checks to make before reporting, none involving the customer.
- "sar_narrative": a draft narrative for a suspicious activity report, at most 170 words: who the subject is, what happened with dates and amounts, and why it is suspicious."""


def load_env():
    llm.load_env()


def config():
    return llm.config()


def status():
    return llm.status()


def brief(trace):
    """The case as the reviewer sees it: only what is on file, trimmed to what matters."""
    cust = trace["customer"]
    docs = [{"id": d["id"], "type": d["type"], "dated": d["dated"], "text": d["text"][:1800]}
            for d in trace["documents"] if not d["baseline"]]
    flagged = [t for t in trace["transactions"] if t["flagged_by"] or t["held"]]
    return {
        "institution": trace["case"]["institution"],
        "case_id": trace["case"]["id"],
        "review_window": [trace["case"]["window_from"], trace["case"]["window_to"]],
        "customer": {k: cust.get(k) for k in ("name", "kind", "occupation", "employer", "business_activity", "onboarded_on", "risk_rating")},
        "declared_at_onboarding": cust.get("declared"),
        "alerts": [{"rule": a["rule_id"], "name": a["rule_name"], "message": a["message"]} for a in trace["alerts"]],
        "flagged_transactions": [{k: t[k] for k in ("id", "at", "direction", "channel", "amount_gbp", "counterparty", "account", "country", "reference", "location", "held")}
                                 for t in flagged[:45]],
        "documents_on_file": docs,
        "internal_list_entries": trace["list_entries"],
        "earlier_reviews": trace["reviews"],
        "first_line_findings": [{"check": c["title"], "outcome": c["outcome"], "finding": f["text"], "code": f["code"]}
                                for c in trace["checks"] for f in c["findings"]],
        "first_line_decision": {k: trace["decision"][k] for k in ("verdict", "reason_code", "reason", "evidence")},
    }


def _parse(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    out = {
        "assessment": data.get("assessment") if data.get("assessment") in ("supports_escalation", "weak_grounds") else "supports_escalation",
        "confidence": data.get("confidence") if data.get("confidence") in ("high", "medium", "low") else "medium",
        "summary": str(data.get("summary") or "").strip(),
        "sar_narrative": str(data.get("sar_narrative") or "").strip(),
    }
    for key in ("strongest_facts", "gaps", "further_checks"):
        value = data.get(key) or []
        out[key] = [str(x).strip() for x in (value if isinstance(value, list) else [value]) if str(x).strip()][:4]
    return out


def review(trace, refresh=False):
    """The supervisor's review of one escalated case. Cached on disk, so each case is paid for once."""
    if trace["decision"]["verdict"] != "ESCALATE":
        return {"error": "The supervisor reviews escalated cases only."}
    cfg = config()
    if not cfg:
        return {"error": "No model key is set.", "setup": status()["setup"]}
    model = cfg["model"]
    user = json.dumps(brief(trace), ensure_ascii=False, indent=1)
    digest = hashlib.sha256((PROMPT_VERSION + model + SYSTEM + user).encode("utf-8")).hexdigest()[:16]
    path = os.path.join(CACHE, trace["case"]["institution"], "{}.{}.json".format(trace["case"]["id"], digest))
    if os.path.exists(path) and not refresh:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    try:
        payload = llm.complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], max_tokens=1300)
        result = _parse(payload["choices"][0]["message"]["content"])
    except llm.LLMError as exc:
        return {"error": exc.message, "detail": exc.detail}
    except (KeyError, IndexError, ValueError) as exc:
        return {"error": "The model's reply could not be read ({}).".format(type(exc).__name__)}
    result["model"] = model
    result["case_id"] = trace["case"]["id"]
    usage = payload.get("usage") or {}
    result["tokens"] = {"in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens")}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)
    return result


def main(argv):
    import argparse
    sys.dont_write_bytecode = True
    sys.path.insert(0, HERE)
    import screening
    parser = argparse.ArgumentParser(description="Review every escalated case at one institution with the AI supervisor.")
    parser.add_argument("institution")
    parser.add_argument("--data", default=os.path.join(HERE, "..", "hackathon", "institutions"))
    parser.add_argument("--limit", type=int, default=0, help="review only the first N escalations")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args(argv[1:])
    if not config():
        sys.exit(status()["setup"])
    desk = screening.Desk(os.path.abspath(args.data))
    ids = [c["id"] for c in desk.queue(args.institution) if c["verdict"] == "ESCALATE"]
    if args.limit:
        ids = ids[:args.limit]
    print("Reviewing {} escalated cases at {} with {} ...".format(len(ids), args.institution, config()["model"]))
    traces = {cid: desk.trace(args.institution, cid) for cid in ids}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = dict(zip(ids, pool.map(lambda cid: review(traces[cid]), ids)))
    errors = {cid: r for cid, r in results.items() if r.get("error")}
    weak = {cid: r for cid, r in results.items() if r.get("assessment") == "weak_grounds"}
    done = len(results) - len(errors)
    tokens_in = sum((r.get("tokens") or {}).get("in") or 0 for r in results.values())
    tokens_out = sum((r.get("tokens") or {}).get("out") or 0 for r in results.values())
    print("Reviewed: {}   supports escalation: {}   weak grounds: {}   errors: {}".format(done, done - len(weak), len(weak), len(errors)))
    if tokens_in:
        print("Tokens used this run: {:,} in, {:,} out".format(tokens_in, tokens_out))
    for cid, r in sorted(weak.items()):
        print("  WEAK GROUNDS {} ({}): {}".format(cid, r.get("confidence"), r.get("summary")))
    for cid, r in sorted(errors.items())[:5]:
        print("  ERROR {}: {} {}".format(cid, r["error"], r.get("detail", "")))


if __name__ == "__main__":
    main(sys.argv)
