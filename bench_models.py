import json, sys, time, urllib.request

KEY = "sk-icJPCq5einsShzVBwbw4Dg"
URL = "http://10.0.0.212:4000/v1/chat/completions"
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
                                          "Content-Type": "application/json"})
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
    for m in sys.argv[1:]:
        probe(m)
