"""Desk assistant: a chat model that answers an analyst's questions by calling the desk's MCP tools.

The model never sees the data directly. Each round it asks for a tool, the request goes through the same MCP
handler an outside agent would use (desk_mcp), and the result is fed back, until it can answer. Every tool call
is reported to the page so the analyst can see what the answer rests on.

Standard library only. Part of the viewer, not of the submitted system.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import desk_mcp
import llm

MAX_ROUNDS = 8
MAX_TOOL_CHARS = 16000

SYSTEM = """You are the assistant on a transaction monitoring screening desk at four UK institutions: bank, emoney, investment and payments_fx. You help an analyst or the MLRO work the alert queue.

A deterministic rule engine has already decided every case as CLOSE, RFI or ESCALATE, with a reason code, a reason and the evidence it relied on. You cannot change a verdict. You can explain one, check it against the case file, compare cases, summarise a queue, and draft notes.

How to work:
- Use the tools for every fact. Never answer about a case, a customer, an amount or a document from memory, and never invent one.
- To find cases use search_cases. To see how a case was decided use screen_case. To check it against the source documents and transactions use get_case_file. For limits and definitions use get_policy.
- Quote case IDs, document IDs and transaction IDs exactly as the tools return them, so the analyst can open them.
- If you are asked to review a case, say whether the grounds hold up, what the strongest facts are, and what the file does not rule out.
- Tipping off: for an ESCALATE case never suggest contacting or questioning the customer. Suggest internal checks only.
- If the tools do not give you the answer, say so plainly.

Style: plain English for a compliance professional. Lead with the answer. Keep it short: a few sentences or a short list, unless asked for a draft. Amounts in pounds as given."""


def model_tools(desk):
    """The MCP tool list, in the shape chat models expect."""
    listed = desk_mcp.handle(desk, {"jsonrpc": "2.0", "id": 0, "method": "tools/list"})["result"]["tools"]
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}}
            for t in listed]


def call_mcp(desk, name, arguments, call_id):
    """Run one tool through the MCP handler. Returns (text, ok)."""
    answer = desk_mcp.handle(desk, {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})
    if "error" in answer:
        return answer["error"]["message"], False
    result = answer["result"]
    text = "\n".join(part.get("text", "") for part in result.get("content", []))
    return text, not result.get("isError")


def _summary(name, text, ok):
    if not ok:
        return text[:160]
    try:
        data = json.loads(text)
    except ValueError:
        return "{:,} characters".format(len(text))
    if name == "search_cases":
        return "{} matching cases, {} returned".format(data.get("matches"), data.get("returned"))
    if name == "screen_case":
        return "{} · {}".format(data["decision"]["verdict"], data["decision"]["reason_code"])
    if name == "get_case_file":
        return "{} transactions, {} documents".format(len(data.get("transactions", [])), len(data.get("documents", [])))
    if name == "queue_overview":
        return "{} cases".format(data.get("cases"))
    if name == "list_institutions":
        return "{} institutions".format(len(data))
    if name == "get_policy":
        return "policy text, {:,} characters".format(len(data.get("policy", "")))
    return "{:,} characters".format(len(text))


def run(desk, history, institution=None, case_id=None):
    """Answer the latest message. Yields events for the page: tool, tool_result, final or error."""
    cfg = llm.config()
    if not cfg:
        yield {"type": "error", "message": "No model key is set.", "detail": llm.SETUP}
        return
    context = "The analyst currently has the {} queue open".format(institution) if institution else ""
    if case_id:
        context += " and is looking at case {}. \"This case\" means that one.".format(case_id)
    elif context:
        context += "."
    messages = [{"role": "system", "content": SYSTEM + ("\n\n" + context if context else "")}]
    for m in history[-12:]:
        if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip():
            messages.append({"role": m["role"], "content": m["content"]})
    tools = model_tools(desk)
    calls = 0
    try:
        for _ in range(MAX_ROUNDS):
            reply = llm.complete(messages, tools=tools, max_tokens=1400)
            message = reply["choices"][0]["message"]
            wanted = message.get("tool_calls") or []
            if not wanted:
                yield {"type": "final", "reply": (message.get("content") or "").strip() or "I could not produce an answer.",
                       "model": cfg["model"], "provider": cfg["label"], "tool_calls": calls}
                return
            messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": wanted})
            for call in wanted:
                name = (call.get("function") or {}).get("name") or ""
                try:
                    arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                calls += 1
                yield {"type": "tool", "name": name, "arguments": arguments}
                text, ok = call_mcp(desk, name, arguments if isinstance(arguments, dict) else {}, calls)
                yield {"type": "tool_result", "name": name, "ok": ok, "summary": _summary(name, text, ok)}
                if len(text) > MAX_TOOL_CHARS:
                    text = text[:MAX_TOOL_CHARS] + "\n... (truncated; ask for something narrower)"
                messages.append({"role": "tool", "tool_call_id": call.get("id") or str(calls), "name": name, "content": text})
        yield {"type": "final", "reply": "I used the maximum number of tool calls without reaching an answer. Try a narrower question.",
               "model": cfg["model"], "provider": cfg["label"], "tool_calls": calls}
    except llm.LLMError as exc:
        yield {"type": "error", "message": exc.message, "detail": exc.detail}
    except Exception as exc:
        yield {"type": "error", "message": "The assistant failed: {}".format(type(exc).__name__), "detail": str(exc)}


# --------------------------------------------------------------------------------------------------------------
# Grok, connecting to the desk from outside
# --------------------------------------------------------------------------------------------------------------

def grok_ready():
    """What is needed for Grok to reach the desk itself: an xAI key, a public address for the connector and its token."""
    llm.load_env()
    env = os.environ
    return {"key": bool(env.get("XAI_API_KEY", "").strip()), "public_url": env.get("MCP_PUBLIC_URL", "").strip().rstrip("/"),
            "token": bool(env.get("MCP_TOKEN", "").strip()), "model": env.get("GROK_MODEL", "grok-4.7")}


def grok_enabled():
    ready = grok_ready()
    return ready["key"] and bool(ready["public_url"]) and ready["token"] and os.environ.get("ASSISTANT", "grok").lower() != "local"


def _grok_request(messages, ready):
    body = {"model": ready["model"], "input": messages, "tools": [{
        "type": "mcp", "server_url": ready["public_url"] + "/mcp", "server_label": "screening_desk",
        "server_description": "Transaction monitoring alert queue: verdicts, reasons, evidence and case files. Read-only.",
        "authorization": os.environ["MCP_TOKEN"].strip()}]}
    request = urllib.request.Request("https://api.x.ai/v1/responses", data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": "Bearer " + os.environ["XAI_API_KEY"].strip(), "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.loads(response.read().decode("utf-8"))


def _grok_text(payload):
    """The answer text and the tool calls Grok reports, from a Responses API reply."""
    texts, calls = [], []
    for item in payload.get("output") or []:
        kind = item.get("type") or ""
        if kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("text"):
                    texts.append(part["text"])
        elif "call" in kind and item.get("name"):
            calls.append(item.get("name"))
    if not texts and payload.get("output_text"):
        texts.append(payload["output_text"])
    return "\n".join(texts).strip(), calls


def run_grok(history, institution=None, case_id=None, activity=None):
    """Ask Grok. xAI's servers call this desk's connector over the internet; each call it makes is reported as it arrives.

    `activity(since)` returns the connector's log entries from index `since` on, so the page can show Grok's calls live.
    """
    ready = grok_ready()
    context = "The analyst currently has the {} queue open".format(institution) if institution else ""
    if case_id:
        context += " and is looking at case {}. \"This case\" means that one.".format(case_id)
    elif context:
        context += "."
    turns = [{"role": m["role"], "content": m["content"]} for m in history[-12:]
             if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()]
    guide = SYSTEM + ("\n\n" + context if context else "")
    box = {}

    def work():
        try:
            try:
                box["payload"] = _grok_request([{"role": "system", "content": guide}] + turns, ready)
            except urllib.error.HTTPError as exc:
                if exc.code != 400:
                    raise
                merged = list(turns)   # some deployments take no system message: fold the guidance into the first turn
                merged[0] = {"role": merged[0]["role"], "content": guide + "\n\n" + merged[0]["content"]}
                box["payload"] = _grok_request(merged, ready)
        except urllib.error.HTTPError as exc:
            box["error"] = ("Grok returned HTTP {}.".format(exc.code), exc.read().decode("utf-8", "replace")[:400])
        except Exception as exc:
            box["error"] = ("Grok could not be reached.", "{}: {}".format(type(exc).__name__, exc))

    start = len(activity(0)) if activity else 0
    seen = 0
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    while True:
        alive = thread.is_alive()
        for entry in (activity(start) if activity else [])[seen:]:
            seen += 1
            if entry.get("tool"):
                yield {"type": "tool", "name": entry["tool"], "arguments": entry.get("arguments") or {}}
                yield {"type": "tool_result", "name": entry["tool"], "ok": True, "summary": "called by Grok over the internet"}
        if not alive:
            break
        time.sleep(0.3)
    if "error" in box:
        yield {"type": "error", "message": box["error"][0], "detail": box["error"][1]}
        return
    text, calls = _grok_text(box.get("payload") or {})
    yield {"type": "final", "reply": text or "Grok returned no answer.", "model": ready["model"],
           "provider": "xAI, through the MCP connector", "tool_calls": max(seen, len(calls))}
