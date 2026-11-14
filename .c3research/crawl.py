import json, urllib.request, urllib.parse, concurrent.futures, sys, collections

BASE = "http://10.0.0.132:3000/api/v1/repos/hnasher1/c3rberus/contents/"

def walk(p):
    url = BASE + urllib.parse.quote(p)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                d = json.load(r)
            break
        except Exception as e:
            err = f"{p!r}: {e}"
            d = None
    if d is None:
        return [], [], [err]
    if isinstance(d, dict):
        return [], [], [f"{p!r}: dict response keys={list(d)[:5]}"]
    out, subs, errs = [], [], []
    for e in d:
        if not isinstance(e, dict):
            errs.append(f"{p!r}: bad entry {e!r}")
            continue
        fp = (p + "/" + str(e.get("name", ""))).lstrip("/")
        t = e.get("type")
        if t == "dir":
            subs.append(fp)
        else:
            out.append((fp, e.get("size", 0)))
    return out, subs, errs

frontier = [""]
results, errs = [], []
with concurrent.futures.ThreadPoolExecutor(10) as ex:
    while frontier:
        batch = list(ex.map(walk, frontier))
        frontier = []
        for out, subs, e in batch:
            results.extend(out)
            errs.extend(e)
            frontier.extend(subs)

results.sort()
with open("allfiles.txt", "w") as f:
    for p, s in results:
        f.write(f"{s}\t{p}\n")
print("total files:", len(results), "errors:", len(errs))
for e in errs[:20]:
    print("ERR", e)
c = collections.Counter(p.split("/")[0] for p, _ in results)
for k, v in sorted(c.items(), key=lambda x: -x[1]):
    print(f"{v:6} {k}")
