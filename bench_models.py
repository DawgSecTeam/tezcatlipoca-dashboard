import json, os, sys, time, urllib.request

KEY = os.environ.get("TEZ_LITELLM_API_KEY") or ""
URL = "https://litellm.dawgsec.com/v1/chat/completions"
MODELS = ("clef-flash", "k2", "qwen3.8-flash-next", "qwen3.8-27b")
TOOLS = [{"type": "function", "function": {"name": "bash", "description": "run a shell command",
          "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}},
                         "required": ["cmd"]}}}]

def probe(model, tools=True):
    body = {"model": model, "max_tokens": 300,
            "messages": [{"role": "user",
                          "content": "Use the bash tool to run: echo READY"}]}
    if tools:
        body["tools"] = TOOLS
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json",
                                          # Cloudflare fronts the gateway; its default
                                          # urllib UA is refused (403 error 1010)
                                          "User-Agent": "tezcatlipoca-bench/1.0"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        ms = int((time.time() - t) * 1000)
        m = (d.get("choices") or [{}])[0].get("message") or {}
        tc = m.get("tool_calls") or []
        names = ",".join(c.get("function", {}).get("name", "?") for c in tc)
        content = (m.get("content") or "").replace("\n", " ")[:50]
        print(f"{model:32s} {ms:6d}ms tools={len(tc)} [{names}] content={content!r}")
        return ms, len(tc)
    except Exception as e:
        print(f"{model:32s} FAILED: {str(e)[:90]}")
        return None, 0

if __name__ == "__main__":
    if not KEY:
        sys.exit("TEZ_LITELLM_API_KEY is not set (the litellm gateway key)")
    for m in sys.argv[1:] or MODELS:
        probe(m)
