#!/usr/bin/env python3
"""MCP connector for the screening desk: lets an AI agent (Grok, Cursor, Claude and others) use the desk as tools.

Two transports, same tools:

  Streamable HTTP   the viewer serves it at  http://127.0.0.1:8765/mcp   (python3 ui/server.py)
  stdio             python3 ui/desk_mcp.py   for clients that launch a local process

Every tool is read-only. Verdicts are set by the rule engine and cannot be changed through the connector.
Standard library only. This is part of the viewer, not of the submitted system.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_INFO = {"name": "screening-desk", "title": "Screening desk", "version": "1.0.0"}
PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = (
    "Read-only access to a transaction monitoring alert queue at four UK institutions (bank, emoney, investment, payments_fx). "
    "A deterministic rule engine has already screened every case as CLOSE, RFI or ESCALATE, with a reason and the evidence it relied on. "
    "Start with list_institutions, find cases with search_cases, read one with screen_case, and open its documents with get_case_file. "
    "You cannot change a verdict. On an ESCALATE case never suggest contacting or questioning the customer: that risks tipping off."
)

SUPERVISOR_PROMPT = (
    "Act as the second-line reviewer (deputy to the MLRO) for case {case_id} at {institution}. "
    "Call screen_case and get_case_file for it. Then, using only what those tools return: "
    "1) say whether the grounds for the verdict hold up; 2) list the strongest facts with their document and transaction IDs; "
    "3) list what the file does not rule out; 4) suggest further checks that do not involve the customer; "
    "5) if the verdict is ESCALATE, draft a suspicious activity report narrative of at most 170 words. "
    "Do not invent facts, and do not change the verdict."
)


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


INSTITUTION = {"type": "string", "description": "Institution folder name: bank, emoney, investment or payments_fx."}
CASE_ID = {"type": "string", "description": "Case ID exactly as listed, for example BK-CS125943."}

TOOLS = [
    {"name": "list_institutions", "title": "List institutions",
     "description": "List the institutions on the desk with how many cases each has and how many were closed, sent for information (RFI) or escalated.",
     "inputSchema": _schema({})},
    {"name": "queue_overview", "title": "Queue overview",
     "description": "Outcomes for one institution's queue: counts by verdict, by reason code and by the monitoring rule that fired.",
     "inputSchema": _schema({"institution": INSTITUTION}, ["institution"])},
    {"name": "search_cases", "title": "Search cases",
     "description": "Find cases in one institution's queue. Filter by verdict (CLOSE, RFI, ESCALATE), by rule (for example TM22), by reason code, "
                    "or by text in the case ID or customer name. Returns case ID, customer, rules fired, verdict and reason code.",
     "inputSchema": _schema({
         "institution": INSTITUTION,
         "verdict": {"type": "string", "enum": ["CLOSE", "RFI", "ESCALATE"]},
         "rule": {"type": "string", "description": "Rule ID such as TM01 or TM22."},
         "reason_code": {"type": "string", "description": "Reason code such as ESC_MULE_ACTIVITY."},
         "query": {"type": "string", "description": "Text to find in the case ID or customer name."},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum rows to return (default 20)."},
     }, ["institution"])},
    {"name": "screen_case", "title": "Screen a case",
     "description": "How one case was decided: the customer, each alert and whether it was explained, the checks that found something, "
                    "the verdict with its reason code, reason and evidence IDs, and the analyst's next step (drafted request, MLRO referral or closing note).",
     "inputSchema": _schema({"institution": INSTITUTION, "case_id": CASE_ID}, ["institution", "case_id"])},
    {"name": "get_case_file", "title": "Get the case file",
     "description": "The file behind one case: the transactions the alerts fired on, the full text of every supporting document on file, "
                    "earlier reviews and any internal list entry. Use it to check a verdict against the source material.",
     "inputSchema": _schema({"institution": INSTITUTION, "case_id": CASE_ID}, ["institution", "case_id"])},
    {"name": "get_policy", "title": "Get the policy",
     "description": "The institution's transaction monitoring SOP: verdict definitions, the rules it runs with their limits, and its policy settings.",
     "inputSchema": _schema({"institution": INSTITUTION}, ["institution"])},
]
for _tool in TOOLS:
    _tool["annotations"] = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

PROMPTS = [{
    "name": "second_line_review", "title": "Second-line review of a case",
    "description": "Review one case as the MLRO's deputy: test the grounds, list gaps, suggest checks and draft the report narrative.",
    "arguments": [{"name": "institution", "description": "bank, emoney, investment or payments_fx", "required": True},
                  {"name": "case_id", "description": "Case ID", "required": True}],
}]


class ToolError(Exception):
    pass


def _institution(desk, args):
    name = (args or {}).get("institution")
    if name not in desk.names:
        raise ToolError("Unknown institution {!r}. Use one of: {}.".format(name, ", ".join(desk.names)))
    return name


def _trace(desk, args):
    name = _institution(desk, args)
    trace = desk.trace(name, (args or {}).get("case_id") or "")
    if not trace:
        raise ToolError("Unknown case {!r} at {}. Use search_cases to find case IDs.".format((args or {}).get("case_id"), name))
    return trace


def _count(rows, key):
    out = {}
    for row in rows:
        out[row[key]] = out.get(row[key], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def call_tool(desk, name, args):
    if name == "list_institutions":
        return [{"institution": n, "cases": len(desk.queue(n)), "by_verdict": _count(desk.queue(n), "verdict")} for n in desk.names]
    if name == "queue_overview":
        inst = _institution(desk, args)
        rows, rules = desk.queue(inst), desk.rules(inst)
        by_rule = {}
        for r in rows:
            for rule in r["rules"]:
                entry = by_rule.setdefault(rule, {"name": rules.get(rule, rule), "cases": 0, "CLOSE": 0, "RFI": 0, "ESCALATE": 0})
                entry["cases"] += 1
                entry[r["verdict"]] += 1
        return {"institution": inst, "cases": len(rows), "by_verdict": _count(rows, "verdict"),
                "by_reason_code": _count(rows, "reason_code"), "by_rule": dict(sorted(by_rule.items()))}
    if name == "search_cases":
        inst = _institution(desk, args)
        q = (args.get("query") or "").lower()
        rows = [r for r in desk.queue(inst)
                if (not args.get("verdict") or r["verdict"] == args["verdict"])
                and (not args.get("rule") or args["rule"] in r["rules"])
                and (not args.get("reason_code") or r["reason_code"] == args["reason_code"])
                and (not q or q in (r["id"] + " " + r["customer"]).lower())]
        limit = max(1, min(100, int(args.get("limit") or 20)))
        return {"institution": inst, "matches": len(rows), "returned": min(limit, len(rows)),
                "cases": [{"case_id": r["id"], "customer": r["customer"], "rules": r["rules"], "verdict": r["verdict"],
                           "reason_code": r["reason_code"], "held_payment": r["held"]} for r in rows[:limit]]}
    if name == "screen_case":
        t = _trace(desk, args)
        cust = t["customer"]
        return {
            "case": t["case"],
            "customer": {k: cust.get(k) for k in ("id", "name", "kind", "occupation", "employer", "business_activity", "risk_rating", "onboarded_on")},
            "declared_at_onboarding": cust.get("declared"),
            "alerts": [{"rule": a["rule_id"], "name": a["rule_name"], "message": a["message"],
                        "explained": a["test"]["explained"], "test": a["test"]["note"]} for a in t["alerts"]],
            "checks_that_found_something": [{"check": c["title"], "outcome": c["outcome"],
                                             "findings": [{"reason_code": f["code"], "text": f["text"], "evidence": f["evidence"]} for f in c["findings"]],
                                             "blocks": c["blocks"]} for c in t["checks"] if c["outcome"] != "nothing"],
            "checks_run": len(t["checks"]),
            "decision": t["decision"],
            "next_step": t["next"],
        }
    if name == "get_case_file":
        t = _trace(desk, args)
        return {
            "case_id": t["case"]["id"],
            "transactions": [x for x in t["transactions"] if x["flagged_by"] or x["held"]],
            "other_transfers_in_window": [x for x in t["transactions"] if not (x["flagged_by"] or x["held"])],
            "documents": [{"id": d["id"], "type": d["type"], "title": d["title"], "dated": d["dated"],
                           "relied_on": d["relied_on"], "text": None if d["baseline"] else d["text"]} for d in t["documents"]],
            "earlier_reviews": t["reviews"],
            "internal_list_entries": t["list_entries"],
        }
    if name == "get_policy":
        inst = _institution(desk, args)
        return {"institution": inst, "policy": desk.policy(inst)}
    return None


def handle(desk, message):
    """Answer one JSON-RPC message. Returns the response, or None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message or method is None:
        return None   # a notification, or a response to something we never asked
    mid = message["id"]

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def error(code, text):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}

    if method == "initialize":
        wanted = params.get("protocolVersion")
        return ok({"protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
                   "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if name not in [t["name"] for t in TOOLS]:
            return error(-32602, "Unknown tool: {}".format(name))
        try:
            result = call_tool(desk, name, params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}], "isError": False})
        except ToolError as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
        except Exception as exc:   # report it to the agent rather than drop the connection
            return ok({"content": [{"type": "text", "text": "The tool failed: {}: {}".format(type(exc).__name__, exc)}], "isError": True})
    if method == "prompts/list":
        return ok({"prompts": PROMPTS})
    if method == "prompts/get":
        if params.get("name") != "second_line_review":
            return error(-32602, "Unknown prompt: {}".format(params.get("name")))
        a = params.get("arguments") or {}
        text = SUPERVISOR_PROMPT.format(case_id=a.get("case_id", "<case_id>"), institution=a.get("institution", "<institution>"))
        return ok({"description": PROMPTS[0]["description"], "messages": [{"role": "user", "content": {"type": "text", "text": text}}]})
    if method in ("resources/list", "resources/templates/list"):
        return ok({"resources": []} if method == "resources/list" else {"resourceTemplates": []})
    return error(-32601, "Method not found: {}".format(method))


def handle_payload(desk, payload):
    """Handle a single message or a batch. Returns (response or None)."""
    if isinstance(payload, list):
        answers = [a for a in (handle(desk, m) for m in payload) if a is not None]
        return answers or None
    return handle(desk, payload)


def main():
    """stdio transport: one JSON-RPC message per line on stdin, one response per line on stdout."""
    sys.dont_write_bytecode = True
    sys.path.insert(0, HERE)
    import screening
    data = os.environ.get("DESK_DATA") or os.path.join(HERE, "..", "hackathon", "institutions")
    if len(sys.argv) > 2 and sys.argv[1] == "--data":
        data = sys.argv[2]
    desk = screening.Desk(os.path.abspath(data))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            answer = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            answer = handle_payload(desk, payload)
        if answer is not None:
            sys.stdout.write(json.dumps(answer, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
