#!/usr/bin/env python3
"""One place that knows how to reach a chat model. Used by the AI supervisor and the desk assistant.

Any provider with an OpenAI-style chat completions endpoint works. Set one key in the environment or in
ui/.env (which git ignores):

    GLM_API_KEY=...            Z.ai / Zhipu GLM      default model glm-5.3
    OPENROUTER_API_KEY=...     OpenRouter            default model x-ai/grok-4.7
    XAI_API_KEY=...            xAI (Grok) directly   default model grok-4.7

    LLM_MODEL=...              optional: use a different model
    LLM_PROVIDER=glm|openrouter|xai     optional: choose when more than one key is set
    LLM_API_URL=... LLM_API_KEY=...     optional: any other compatible endpoint

Check the connection:   python3 ui/llm.py --check

Standard library only. Part of the viewer, not of the submitted system.
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

PROVIDERS = [
    # name, label, key variables, endpoints to try in order, default model
    ("glm", "Z.ai GLM", ["GLM_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY", "BIGMODEL_API_KEY"],
     ["https://api.z.ai/api/paas/v4/chat/completions", "https://open.bigmodel.cn/api/paas/v4/chat/completions"], "glm-5.3"),
    ("openrouter", "OpenRouter", ["OPENROUTER_API_KEY"], ["https://openrouter.ai/api/v1/chat/completions"], "x-ai/grok-4.7"),
    ("xai", "xAI", ["XAI_API_KEY"], ["https://api.x.ai/v1/chat/completions"], "grok-4.7"),
]
SETUP = "Add a model key to ui/.env (GLM_API_KEY=..., OPENROUTER_API_KEY=... or XAI_API_KEY=...) and restart the viewer."
_working_url = {}


class LLMError(Exception):
    def __init__(self, message, detail=""):
        super().__init__(message)
        self.message, self.detail = message, detail


def load_env():
    """Read KEY=VALUE lines from ui/.env without overriding what is already set."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if value:
                    os.environ.setdefault(key.strip(), value)


def config():
    """The provider to use: {provider, label, urls, key, model}, or None when no key is set."""
    load_env()
    env = os.environ
    model = env.get("LLM_MODEL") or env.get("SUPERVISOR_MODEL")
    url = env.get("LLM_API_URL") or env.get("SUPERVISOR_API_URL")
    key = env.get("LLM_API_KEY") or env.get("SUPERVISOR_API_KEY")
    if url and key:
        return {"provider": "custom", "label": "custom endpoint", "urls": [url], "key": key, "model": model or "glm-5.3"}
    found = []
    for name, label, variables, urls, default in PROVIDERS:
        value = next((env[v].strip() for v in variables if env.get(v, "").strip()), None)
        if value:
            found.append({"provider": name, "label": label, "urls": urls, "key": value, "model": model or default})
    wanted = env.get("LLM_PROVIDER", "").strip().lower()
    return next((f for f in found if f["provider"] == wanted), found[0] if found else None)


def status():
    cfg = config()
    return {"enabled": bool(cfg), "model": cfg["model"] if cfg else None, "provider": cfg["label"] if cfg else None,
            "setup": None if cfg else SETUP}


def complete(messages, tools=None, max_tokens=1200, temperature=0.1):
    """One chat completion. Returns the provider's JSON reply; raises LLMError with a plain message."""
    cfg = config()
    if not cfg:
        raise LLMError("No model key is set.", SETUP)
    body = {"model": cfg["model"], "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if cfg["provider"] == "glm":
        # Current GLM models always reason; "low" keeps the assistant quick. Use high or max for harder questions.
        body["reasoning_effort"] = os.environ.get("GLM_REASONING", "low")
    urls = list(cfg["urls"])
    if _working_url.get(cfg["provider"]) in urls:
        urls.sort(key=lambda u: u != _working_url[cfg["provider"]])
    last = None
    for i, url in enumerate(urls):
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
            "Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json", "X-Title": "Screening desk"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not payload.get("choices"):
                raise LLMError("The model returned no answer.", json.dumps(payload)[:300])
            _working_url[cfg["provider"]] = url
            return payload
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            last = LLMError("The model provider ({}) returned HTTP {}.".format(cfg["label"], exc.code), detail)
            if exc.code in (401, 403, 404) and i + 1 < len(urls):
                continue   # a key for the provider's other platform
            raise last
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = LLMError("Could not reach the model provider ({}).".format(cfg["label"]), str(exc))
            if i + 1 < len(urls):
                continue
            raise last
        except ValueError as exc:
            raise LLMError("The model provider's reply could not be read.", str(exc))
    raise last or LLMError("The model could not be reached.")


def main():
    if "--check" not in sys.argv:
        sys.exit(__doc__)
    cfg = config()
    if not cfg:
        sys.exit("No key found. " + SETUP)
    print("Provider: {}   model: {}   key: {} characters".format(cfg["label"], cfg["model"], len(cfg["key"])))
    try:
        reply = complete([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=20)
        print("Endpoint: {}".format(_working_url.get(cfg["provider"], cfg["urls"][0])))
        print("Plain reply: {!r}".format((reply["choices"][0]["message"].get("content") or "").strip()))
        tools = [{"type": "function", "function": {"name": "list_institutions", "description": "List the institutions on the desk.",
                                                   "parameters": {"type": "object", "properties": {}}}}]
        reply = complete([{"role": "user", "content": "Which institutions are on the desk? Use the tool."}], tools=tools, max_tokens=200)
        calls = reply["choices"][0]["message"].get("tool_calls") or []
        print("Tool calling: {}".format("works, asked for " + calls[0]["function"]["name"] if calls else "the model did not call the tool"))
        usage = reply.get("usage") or {}
        print("Tokens on the last call: {} in, {} out".format(usage.get("prompt_tokens"), usage.get("completion_tokens")))
    except LLMError as exc:
        sys.exit("FAILED: {} {}".format(exc.message, exc.detail))


if __name__ == "__main__":
    main()
