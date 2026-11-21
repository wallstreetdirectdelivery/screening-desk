#!/usr/bin/env python3
"""Screening desk: a local viewer for the alert-closing system, and an MCP connector for AI agents.

    python3 ui/server.py [--data hackathon/institutions] [--port 8765]

Then open http://127.0.0.1:8765. Standard library only. It listens on this machine only and reads the
institution folders. It writes nothing, except a cache of AI supervisor reviews (ui/.cache) if that optional
feature is switched on with a model key; see supervisor.py.

AI agents connect to http://127.0.0.1:8765/mcp (Model Context Protocol, Streamable HTTP); see desk_mcp.py.
To let a hosted agent such as Grok reach it, put a tunnel in front and set MCP_TOKEN in ui/.env. Requests that
arrive through a tunnel can only reach /mcp, and only with that token.
"""
import argparse
import collections
import datetime
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent  # noqa: E402
import desk_mcp  # noqa: E402
import llm  # noqa: E402
import screening  # noqa: E402
import supervisor  # noqa: E402

DESK = None
ACTIVITY = collections.deque(maxlen=200)   # what has called the connector, newest last


def note_activity(payload, remote):
    for message in (payload if isinstance(payload, list) else [payload]):
        if not isinstance(message, dict) or not message.get("method"):
            continue
        params = message.get("params") or {}
        ACTIVITY.append({
            "at": datetime.datetime.now().strftime("%H:%M:%S"), "remote": remote, "method": message["method"],
            "tool": params.get("name") if message["method"] == "tools/call" else None,
            "arguments": params.get("arguments") if message["method"] == "tools/call" else None,
            "client": ((params.get("clientInfo") or {}).get("name") if message["method"] == "initialize" else None)})
FORWARDED = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip", "cf-connecting-ip", "cf-ray")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, body, content_type, extra=None):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, obj, status=200, extra=None):
        self._send(status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", extra)

    # ---- who is asking

    def _remote(self):
        """True when the request did not come straight from this machine (for example through a tunnel)."""
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return True
        return any(self.headers.get(h) for h in FORWARDED)

    def _origin_ok(self):
        """Browsers send Origin; only pages served from this machine may call the desk (DNS rebinding guard)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = urlparse(origin).hostname or ""
        allowed = [h.strip() for h in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if h.strip()]
        return host in LOCAL_HOSTS or origin in allowed or host in allowed

    def _mcp_allowed(self):
        """(ok, status, message). A token is required for anything that is not local."""
        token = os.environ.get("MCP_TOKEN", "").strip()
        given = (self.headers.get("Authorization") or "").strip()
        if given.lower().startswith("bearer "):
            given = given[7:].strip()
        if token:
            if given and hmac.compare_digest(given.encode(), token.encode()):
                return True, 200, ""
            if not self._remote() and not given:
                return True, 200, ""    # the local viewer and local agents need no token
            return False, 401, "A valid token is required."
        if self._remote():
            return False, 403, "This connector is not open to remote callers. Set MCP_TOKEN in ui/.env and restart."
        return True, 200, ""

    # ---- MCP (Streamable HTTP): one JSON-RPC message per POST, answered as JSON

    def _chat(self):
        """The desk assistant. Streams one JSON object per line as the model calls tools and then answers."""
        if self._remote() or not self._origin_ok():
            return self._json({"error": "The assistant is only available on the machine the viewer runs on."}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "bad request"}, 400)
        inst = body.get("inst") if body.get("inst") in DESK.names else None
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        case_id = body.get("case_id") if inst else None
        if agent.grok_enabled():
            events = agent.run_grok(body.get("messages") or [], inst, case_id, activity=lambda since: [a for a in list(ACTIVITY)[since:] if a["remote"]])
        else:
            events = agent.run(DESK, body.get("messages") or [], inst, case_id)
        try:
            for event in events:
                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/api/chat":
            return self._chat()
        if url.path.rstrip("/") != "/mcp":
            return self._json({"error": "not found"}, 404)
        if not self._origin_ok():
            return self._json({"error": "origin not allowed"}, 403)
        ok, status, message = self._mcp_allowed()
        if not ok:
            extra = {"WWW-Authenticate": "Bearer"} if status == 401 else None
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": message}}, status, extra)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else None
        except (ValueError, UnicodeDecodeError):
            return self._json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
        note_activity(payload, self._remote())
        answer = desk_mcp.handle_payload(DESK, payload)
        if answer is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(answer)

    def do_DELETE(self):
        self._send(405, "", "text/plain", {"Allow": "POST"})

    def do_OPTIONS(self):
        if not self._origin_ok():
            return self._send(403, "", "text/plain")
        self._send(204, "", "text/plain", {
            "Access-Control-Allow-Origin": self.headers.get("Origin") or "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization, Accept, Mcp-Session-Id, MCP-Protocol-Version"})

    # ---- the viewer

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        inst = (query.get("inst") or [""])[0]
        if url.path.rstrip("/") == "/mcp":
            return self._send(405, "", "text/plain", {"Allow": "POST"})   # no server-initiated stream is offered
        if self._remote():
            return self._json({"error": "The viewer is only available on the machine it runs on. Agents connect at /mcp."}, 403)
        try:
            if url.path in ("/", "/index.html", "/bot", "/bot.html"):
                page = "bot.html" if url.path.startswith("/bot") else "index.html"
                with open(os.path.join(HERE, page), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if url.path == "/api/institutions":
                return self._json({"institutions": DESK.names})
            if url.path == "/api/cases":
                if inst not in DESK.names:
                    return self._json({"error": "unknown institution"}, 404)
                return self._json({"institution": inst, "cases": DESK.queue(inst), "rules": DESK.rules(inst)})
            if url.path == "/api/case":
                if inst not in DESK.names:
                    return self._json({"error": "unknown institution"}, 404)
                result = DESK.trace(inst, (query.get("id") or [""])[0])
                return self._json(result) if result else self._json({"error": "unknown case"}, 404)
            if url.path == "/api/connect":
                return self._json({
                    "mcp_path": "/mcp", "token_set": bool(os.environ.get("MCP_TOKEN", "").strip()),
                    "stdio": ["python3", os.path.join(HERE, "desk_mcp.py")],
                    "tools": [{"name": t["name"], "description": t["description"]} for t in desk_mcp.TOOLS],
                    "supervisor": supervisor.status(), "grok": agent.grok_ready(), "grok_enabled": agent.grok_enabled()})
            if url.path == "/api/connect/activity":
                return self._json({"activity": list(ACTIVITY)[-40:]})
            if url.path == "/api/llm/status":
                status = llm.status()
                if agent.grok_enabled():
                    status.update({"enabled": True, "model": agent.grok_ready()["model"], "provider": "xAI", "remote": True})
                return self._json(status)
            if url.path == "/api/supervisor/status":
                return self._json(supervisor.status())
            if url.path == "/api/supervisor":
                if inst not in DESK.names:
                    return self._json({"error": "unknown institution"}, 404)
                result = DESK.trace(inst, (query.get("id") or [""])[0])
                if not result:
                    return self._json({"error": "unknown case"}, 404)
                return self._json(supervisor.review(result, refresh=(query.get("refresh") or [""])[0] == "1"))
            return self._json({"error": "not found"}, 404)
        except Exception as exc:  # show the problem in the page instead of dropping the connection
            return self._json({"error": "{}: {}".format(type(exc).__name__, exc)}, 500)

    def log_message(self, fmt, *args):
        pass


def main():
    global DESK
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=os.path.join(HERE, "..", "hackathon", "institutions"),
                        help="folder holding one sub-folder per institution")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    llm.load_env()   # ui/.env: a model key (GLM_API_KEY, OPENROUTER_API_KEY or XAI_API_KEY), LLM_MODEL, MCP_TOKEN
    DESK = screening.Desk(os.path.abspath(args.data))
    if not DESK.names:
        sys.exit("No institution folders found in " + os.path.abspath(args.data))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Screening desk: http://127.0.0.1:{}  ({})".format(args.port, ", ".join(DESK.names)))
    print("MCP connector:  http://127.0.0.1:{}/mcp  (remote access {})".format(
        args.port, "needs the token in MCP_TOKEN" if os.environ.get("MCP_TOKEN") else "is off until MCP_TOKEN is set"))
    model = llm.status()
    print("Model:          {}".format("{} via {}".format(model["model"], model["provider"]) if model["enabled"] else "none (add a key to ui/.env)"))
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
