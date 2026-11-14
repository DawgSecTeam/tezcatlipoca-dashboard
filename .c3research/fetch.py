#!/usr/bin/env python3
"""Fetch raw files from the C3rberus repo into a local mirror (read-only research)."""
import os, sys, urllib.request, urllib.parse

RAW = "http://10.0.0.132:3000/hnasher1/c3rberus/raw/branch/main/"
MIRROR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")

def get(path):
    url = RAW + urllib.parse.quote(path)
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
            break
        except Exception as e:
            err = e
            data = None
    if data is None:
        print(f"FAIL {path}: {err}", file=sys.stderr)
        return None
    dest = os.path.join(MIRROR, path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)

if __name__ == "__main__":
    tot = 0
    for p in sys.argv[1:]:
        n = get(p)
        if n is not None:
            tot += n
    print(f"fetched {len(sys.argv)-1} files, {tot} bytes")
