"""Deterministic triage: the first-cause answer for a run or the range it acted on.

An operator mid-incident does not need a conversation, they need the sentence that says what
broke and what to do next. The facts that sentence needs are local and cheap: the run record
(state, stage, rc, error), its event log (stage index/total), the run's own log (the driver's
stdout plus the runner's terminal `[dashboard] ...` line), and the range workspace's estate
document. This module derives the answer with rules over those facts, so it is instant,
reproducible and unit-testable — no model is consulted and no engine is polled.

It backs the run page's diagnosis panel and the MCP `diagnose_run`/`diagnose_range` tools: an
agent that needs five round trips (`list_runs` -> `get_run` -> `get_run_log` ->
`get_run_events` -> `get_artifacts`) to reconstruct the same picture gets it in one call.

Secrets: a driver log can quote a command line or a URL carrying a token, and the estate
document carries every generated password. Everything here reads named fields only, quotes a
single redacted log line rather than the tail, and never serializes the document.
"""

import os
import re
import threading
import time
from pathlib import Path

from dashboard import estate, ranges

# How much of a run log a diagnosis reads. The cause is at the end (a driver prints its
# failure last), and reading a whole deploy log is how a triage call turns into a megabyte.
TAIL_BYTES = int(os.environ.get("TEZ_DIAGNOSE_TAIL_BYTES", "200000"))

# A run whose log and stage events have not moved for this long is worth flagging while it
# is still `running` — a hung Proxmox or ssh call is otherwise invisible until it times out.
STALL_SECONDS = float(os.environ.get("TEZ_DIAGNOSE_STALL_SECONDS", "600"))

# A queued run is a worker thread that started immediately and then provisioned; sitting in
# `queued` this long means the worktree fetch/provision is hanging, not that the queue is deep.
QUEUE_WARN_SECONDS = float(os.environ.get("TEZ_DIAGNOSE_QUEUE_SECONDS", "180"))

QUOTE_MAX = 300

# Line counts for a log's prefix, extended across polls (see _lines_before).
_LINE_CACHE = {}
_LINE_LOCK = threading.Lock()

_SEVERITY = {"error": 3, "warning": 2, "info": 1, "ok": 0}

_ACTION_LABELS = {
    "deploy": "Deploy", "plan": "Plan", "scrim": "Scrim", "verify": "Verify",
    "destroy": "Teardown", "redeploy": "Redeploy", "start": "Competition start",
    "freeze": "Scoring pause", "resume": "Scoring resume", "end": "Competition end",
    "delete_team": "Team removal",
}
_STATE_WORDS = {"failed": "failed", "succeeded": "succeeded", "cancelled": "was cancelled",
                "running": "running", "queued": "queued", "pausing": "held", "paused": "held"}

# ── redaction ────────────────────────────────────────────────────────────────────────────
# Best-effort: the goal is that the one line quoted back cannot carry a credential. Every
# pattern keeps the key so the line still reads ("token=<redacted>"), and the quote is capped.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")

_REDACTIONS = (
    (re.compile(r"(?i)(\bBearer\s+)[\w.~+/=-]{8,}"), r"\1<redacted>"),
    (re.compile(r"(?i)\b([\w-]*(?:token|password|passwd|secret|api[_-]?key|private[_-]?key)"
                r"[\w-]*)(['\"]?\s*[=:]\s*)(['\"]?)([^\s'\"]{8,})\3"), r"\1\2\3<redacted>\3"),
    (re.compile(r"(?i)\b(PVEAPIToken=)[^\s,;]+"), r"\1<redacted>"),
    (re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@"), r"\1<redacted>@"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
     "<redacted>"),
)


def redact(text):
    """Strip ANSI and credential-shaped values from a log line, and cap its length."""
    out = _ANSI.sub("", str(text or ""))
    out = "".join(ch for ch in out if ch == "\t" or ch.isprintable())
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > QUOTE_MAX:
        out = out[: QUOTE_MAX - 1].rstrip() + "…"
    return out


# ── the run log ─────────────────────────────────────────────────────────────────────────
_STRONG = re.compile(r"\b(?:[A-Z][A-Za-z0-9_]*(?:Error|Exception|Failure)|Traceback)\b")
_WEAK = re.compile(r"(?i)\b(?:refused|denied|timed out|timeout|not found|no such|no space|"
                   r"unreachable|failed|fatal|critical|interrupted|killed)\b")
_NOISE = re.compile(r"(?i)(urllib3\.exceptions|InsecureRequestWarning|DeprecationWarning|"
                    r"^\s*warnings\.warn\()")
_SUMMARY = re.compile(r"^\[dashboard\]\s+(.+)$")


def _log_tail(run, limit=TAIL_BYTES):
    """(text, from_offset, size, from_line) — the log's last whole lines and where they start.

    Reading starts mid-file and is re-aligned to the next newline, so a quoted line number is
    the file's own line number, not the tail's: the prefix is counted, not assumed."""
    path = run.get("log_path")
    if not path:
        return "", 0, 0, 1
    try:
        size = Path(path).stat().st_size
        if not size:
            return "", 0, 0, 1
        start = max(0, size - limit)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read()
        if start:
            cut = data.find(b"\n")
            if cut >= 0:
                data = data[cut + 1:]
                start += cut + 1
        return (data.decode("utf-8", "replace"), start, size,
                _lines_before(path, start) + 1)
    except OSError:
        return "", 0, 0, 1


def _lines_before(path, end, chunk=1 << 20):
    """Newlines strictly before byte `end`: how many lines precede a tail.

    An open run page polls a diagnosis every few seconds and a deploy log can be tens of
    megabytes, so the prefix count is extended from the last count rather than re-scanned.
    Run logs only grow (`open(..., "w")` once, then appends), and a shrink resets the entry.
    """
    if end <= 0:
        return 0
    key = str(path)
    with _LINE_LOCK:
        base_size, base_lines = _LINE_CACHE.get(key, (0, 0))
        try:
            if end < base_size or Path(path).stat().st_size < base_size:
                base_size, base_lines = 0, 0
        except OSError:
            return 0
        count, read = base_lines, base_size
        with open(path, "rb") as fh:
            fh.seek(base_size)
            while read < end:
                data = fh.read(min(chunk, end - read))
                if not data:
                    break
                count += data.count(b"\n")
                read += len(data)
        if len(_LINE_CACHE) > 256:  # a long-lived service sees many runs; bound the map
            _LINE_CACHE.clear()
        _LINE_CACHE[key] = (read, count)
    return count


def _numbered(text, base=0, first_line=1):
    """(line_no, byte_offset, raw) for each line, with file-absolute numbers and offsets."""
    rows, offset = [], base
    for n, raw in enumerate(text.splitlines(), first_line):
        rows.append((n, offset, raw))
        offset += len(raw.encode("utf-8", "replace")) + 1
    return rows


def find_cause(text, base=0, first_line=1):
    """The line that says what broke: the last error-shaped line before the runner's summary.

    A deploy's log ends with whatever the driver printed last; the runner then appends its
    own `[dashboard] failed rc=...` line. That summary is kept as a fallback, not as the
    cause, because the driver's own exception names the failing call."""
    rows = _numbered(text, base, first_line)
    summary = None
    weak = None
    for n, offset, raw in reversed(rows):
        stripped = raw.strip()
        if not stripped:
            continue
        m = _SUMMARY.match(stripped)
        if m:
            if summary is None:
                summary = {"source": "run-summary", "line": n, "offset": offset,
                           "quote": redact(m.group(1))}
            continue
        if _NOISE.search(raw):
            continue
        quote = redact(raw)
        if not quote:
            continue
        if _STRONG.search(raw):
            return {"source": "run-log", "line": n, "offset": offset, "quote": quote}
        if weak is None and _WEAK.search(raw):
            weak = {"source": "run-log", "line": n, "offset": offset, "quote": quote}
    return weak or summary


# ── rules ───────────────────────────────────────────────────────────────────────────────
# Each rule is a signature an operator would recognise from the driver's own output. They
# are ordered most-specific first: "connection refused" must reach the network rule before
# the preflight rule's broader "refused" can claim it.
_RULES = (
    {"code": "missing_dependency", "severity": "error",
     "title": "A Python dependency is missing in the service venv",
     "match": re.compile(r"ModuleNotFoundError|No module named|ImportError"),
     "detail": "The drivers run under the service's own interpreter, so a venv that predates "
               "a dependency fails inside the deploy instead of at submit.",
     "advice": "Check it with `python3 -c \"import toml, paramiko, mysql.connector\"`, install "
               "`requirements.txt` in that venv, then submit the action again."},
    {"code": "opencode_missing", "severity": "error",
     "title": "opencode is not on the driver's PATH",
     "match": re.compile(r"(?i)(?:opencode[^\n]{0,40}(?:not found|no such file)|"
                         r"(?:not found|no such file)[^\n]{0,40}opencode|"
                         r"opencode: command not found)"),
     "detail": "Every blue cycle is an `opencode run`; without the binary every cycle exits "
               "127 and the range looks undefended.",
     "advice": "Install opencode or set `TEZ_OPENCODE_BIN`; the runner prepends "
               "`~/.opencode/bin` to the driver PATH."},
    {"code": "missing_tool", "severity": "error",
     "title": "A host tool the driver shells out to is missing",
     "match": re.compile(r"(?i)(?:command not found|: not found\b)"),
     "detail": "The drivers call ssh, scp, curl, git and jq, and Windows boxes need "
               "`sshpass`; one absent tool stops the phase that needs it.",
     "advice": "Install the missing tool on the ops host (distro packages plus `sshpass` in "
               "`~/.local/bin`) and submit again."},
    {"code": "ref_not_found", "severity": "error",
     "title": "The pinned engine ref does not resolve",
     "match": re.compile(r"(?i)(?:couldn't find remote ref|unknown revision|invalid ref|"
                         r"cannot fetch engine ref|cannot cut engine worktree|cannot clone the "
                         r"engine repo|not a valid object name)"),
     "detail": "A run is cut from a tezcatlipoca worktree at `engine_ref`; the fetch happens "
               "first, so a stale clone is not the explanation — the ref itself is missing.",
     "advice": "Confirm the branch/SHA/tag exists on origin, or submit with a different "
               "`engine_ref` (empty means the service's pin)."},
    {"code": "ssh_auth", "severity": "error",
     "title": "SSH authentication to a box failed",
     "match": re.compile(r"(?i)(?:permission denied \((?:publickey|password)|"
                         r"host key verification failed|no matching host key|"
                         r"AuthenticationException|authentication failed)"),
     "detail": "Linux boxes are reached with the engine key, Windows boxes over password "
               "auth; a stale key or a rewritten box stops the deploy after its VMs exist.",
     "advice": "Check `TEZ_SSH_PRIVATE_KEY_PATH` and the box credentials recorded in the "
               "workspace; the driver's line above names the target."},
    {"code": "proxmox_auth", "severity": "error",
     "title": "Proxmox rejected the API credentials",
     "match": re.compile(r"(?i)(?:authentication failure|invalid (?:api )?token|"
                         r"401 (?:Unauthorized|Client Error)|403 (?:Forbidden|Client Error)|"
                         r"permission denied for (?:user|token))"),
     "detail": "Every Proxmox call uses the node's token; a token whose privileges or realm "
               "changed fails each call the same way, usually mid-deploy.",
     "advice": "Check the node catalog entry's token and the engine's `TEZ_PROXMOX_*` "
               "settings, then resubmit."},
    {"code": "disk_full", "severity": "error",
     "title": "A host ran out of disk",
     "match": re.compile(r"No space left on device"),
     "detail": "The engine writes templates and clones to the node's storage; a full store "
               "fails the phase that writes, with VMs already recorded.",
     "advice": "Free space on the ops host and the node's VM storage, then tear the partial "
               "range down and redeploy."},
    {"code": "killed", "severity": "error",
     "title": "The driver was killed before it could finish",
     "match": re.compile(r"(?i)(?:out of memory|oom[- ]kill|killed by signal|"
                         r"received SIGKILL)"),
     "detail": "The process died without reporting a failure of its own, so the log ends "
               "mid-phase and the range may hold VMs.",
     "advice": "Check the journal for an OOM kill or a host restart; tear down any recorded "
               "VMs, then resubmit."},
    {"code": "preflight_refused", "severity": "error",
     "title": "The engine refused the run at preflight",
     "match": re.compile(r"(?i)(?:preflight|refus(?:ed|ing) (?:the|this|run|overlap|deploy)|"
                         r"vmid[^\n]{0,40}(?:already|taken|in use)|"
                         r"(?:engine (?:address|ip)|mgmt ip)[^\n]{0,40}(?:already|taken|in use)|"
                         r"already in use by)"),
     "detail": "A deploy's preflight rejects parameters that provably collide: vmids or an "
               "engine management address another range already holds.",
     "advice": "Give this run its own `team_identifiers` and `engine_mgmt_ip`, or tear down "
               "the range holding them."},
    {"code": "network", "severity": "error",
     "title": "The driver could not reach a host it needs",
     "match": re.compile(r"(?i)(?:connection (?:refused|reset|aborted)|timed out|timeout|"
                         r"temporary failure in name resolution|could not resolve|"
                         r"no route to host|max retries exceeded|network is unreachable)"),
     "detail": "A Proxmox, ssh or HTTP call never completed. A dead engine or node looks "
               "exactly like this from inside the driver.",
     "advice": "Check the Proxmox endpoint, the selected nodes and the management network "
               "before retrying; the engine's reachability is the first thing to confirm."},
)


def _match_rule(haystack):
    for rule in _RULES:
        if rule["match"].search(haystack):
            return rule
    return None


# ── findings ────────────────────────────────────────────────────────────────────────────
def _finding(code, severity, title, detail, advice, *, run_id=None, evidence=None):
    return {"code": code, "severity": severity, "title": title, "detail": detail,
            "advice": advice, "run_id": run_id, "evidence": evidence or []}


def _evidence(cause, run, stage):
    """The smallest set of pointers that lets a reader verify the verdict."""
    out = []
    if cause:
        out.append(dict(cause))
    if run.get("error") and (not cause or cause.get("source") != "run-record"):
        out.append({"source": "run-record", "field": "error", "quote": redact(run["error"])})
    if stage and stage.get("label"):
        out.append({"source": "event", "kind": "stage", "quote": redact(stage["label"])})
    return out


def _primary(findings):
    if not findings:
        return None
    return max(findings, key=lambda f: _SEVERITY.get(f["severity"], 0))


def _stage_info(run, events):
    """The furthest stage the run reported, from its own stage events."""
    best = None
    for ev in events:
        if ev.get("kind") != "stage":
            continue
        label = ev.get("stage") or ""
        index, total = ev.get("index"), ev.get("total")
        if index is None or total is None:
            m = re.search(r"phase\s+(\d+)\s*/\s*(\d+)", label)
            if m:
                index, total = int(m.group(1)), int(m.group(2))
        best = {"label": label, "index": index, "total": total, "at": ev.get("ts")}
    if best is None and run.get("stage"):
        label = run["stage"]
        m = re.search(r"phase\s+(\d+)\s*/\s*(\d+)", label)
        best = {"label": label, "index": int(m.group(1)) if m else None,
                "total": int(m.group(2)) if m else None, "at": None}
    return best


def _stage_phrase(run, stage):
    if not stage or not stage.get("label"):
        return ""
    label = stage["label"]
    m = re.match(r"phase\s+(\d+)\s*/\s*(\d+)\s*(.*)$", label)
    if m:
        name = m.group(3).replace("_", " ").strip()
        return f"in phase {m.group(1)}/{m.group(2)}" + (f" ({name})" if name else "")
    return f"during {label}" if run.get("action") not in ("verify", "destroy") else ""


def _last_activity(run, events):
    stamps = [run.get("started_at") or run.get("created_at") or 0.0]
    stamps += [ev.get("ts") or 0.0 for ev in events[-5:]]
    try:
        if run.get("log_path"):
            stamps.append(Path(run["log_path"]).stat().st_mtime)
    except OSError:
        pass
    return max(stamps)


def _run_findings(run, events, tail, cause, stage, now):
    state = run.get("state")
    rc = run.get("returncode")
    error = run.get("error") or ""
    haystack = f"{error}\n{tail}"
    ev = _evidence(cause, run, stage)
    findings = []

    if state == "failed" and error.startswith("orphaned:"):
        # The row outlived the process that was executing it (a service restart); nothing
        # can finish it, and the driver may have done part of the work before it died.
        return [_finding(
            "orphaned_run", "error", "The run was abandoned by a service restart",
            "No worker process survived the restart, so this run can never finish. Its "
            "driver may have completed part of the work before the process died.",
            "Check whether the run recorded VMs before treating it as a no-op: if it did, "
            "tear that range down (or reclaim it by tag) so the orphan cannot collide with "
            "a redeploy. Then submit the action again.",
            run_id=run.get("id"), evidence=ev)]

    if state == "failed":
        # A provisioning refusal (a bad ref, a missing dependency at submit time) reaches the
        # record as the worker's exception with no exit code — still a signature the rule
        # table can name, so it is checked before the generic "crashed" fallback.
        rule = _match_rule(haystack)
        if rule:
            findings.append(_finding(
                rule["code"], rule["severity"], rule["title"], rule["detail"],
                rule["advice"], run_id=run.get("id"), evidence=ev))
        elif rc is None and error:
            findings.append(_finding(
                "worker_crashed", "error", "The run crashed before the driver could report",
                "The failure happened in the service's worker (provisioning or spawn), not in "
                "the engine driver, so there is no driver exit code.",
                "The error above is the whole story; fix it and resubmit. A missing "
                "dependency or a bad ref lands here and is refused before any VM is touched.",
                run_id=run.get("id"), evidence=ev))
        else:
            findings.append(_finding(
                "run_failed", "error", "The driver exited non-zero",
                f"Exit {rc} with no recognisable error signature in the log tail.",
                "Read the quoted line and the log around it; if the cause is not there, the "
                "run's evidence tree is the next place to look.",
                run_id=run.get("id"), evidence=ev))
        return findings

    if state == "cancelled":
        return [_finding(
            "run_cancelled", "info", "The run was cancelled",
            "An operator cancelled it; the driver received the signal and the process group "
            "was killed.",
            "A cancelled deploy can leave a half-built range: check its recorded VMs before "
            "resubmitting.",
            run_id=run.get("id"), evidence=ev)]

    if state in ("paused", "pausing"):
        after = (stage or {}).get("index")
        return [_finding(
            "run_held", "info", "The deploy is held at a phase boundary",
            "Pause-after-next-stage parks the driver at the next phase boundary; the range "
            "stays `building` and the deploy lock stays held.",
            "Resume from the run page (or `resume_run` over MCP) to carry on in the same "
            "process.",
            run_id=run.get("id"), evidence=ev)]

    if state == "running":
        idle = now - _last_activity(run, events)
        if idle >= STALL_SECONDS:
            return [_finding(
                "run_stalled", "warning",
                f"No output or stage change for {_duration(idle)}",
                "Neither the log nor the stage events have moved. A long Proxmox clone can "
                "legitimately print nothing, so this is a prompt to look, not a failure.",
                "Check the log tail and the node's task list before cancelling: a deploy stuck "
                "in an API call often clears itself, while a cancel mid-phase leaves VMs.",
                run_id=run.get("id"), evidence=ev)]
        return []

    if state == "queued":
        waited = now - (run.get("created_at") or now)
        if waited >= QUEUE_WARN_SECONDS:
            return [_finding(
                "run_queued_long", "warning", "Provisioning has not started",
                "The worker thread starts with the submit and then provisions; staying "
                "`queued` this long means the fetch or workspace seed is hanging.",
                "Check `run.log` (it starts with the engine ref and the cut command) and the "
                "engine repo clone; cancel and resubmit once the fetch works.",
                run_id=run.get("id"), evidence=ev)]
        return []

    return findings


def _duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _run_headline(run, stage, primary):
    action = _ACTION_LABELS.get(run.get("action"), run.get("action") or "Run")
    word = _STATE_WORDS.get(run.get("state"), run.get("state") or "")
    phrase = _stage_phrase(run, stage)
    head = f"{action} {word}{' ' + phrase if phrase else ''}".strip()
    if primary and primary["severity"] in ("error", "warning"):
        return f"{head}: {primary['title']}"
    return head


# ── public surface ──────────────────────────────────────────────────────────────────────
def diagnose_run(store, run, now=None):
    """A run's triage answer: headline, first cause, findings, and the facts behind them."""
    now = time.time() if now is None else now
    _, events = store.get_events(run["id"], 0)
    tail, from_offset, size, from_line = _log_tail(run)
    cause = find_cause(tail, from_offset, from_line)
    stage = _stage_info(run, events)
    findings = _run_findings(run, events, tail, cause, stage, now)
    primary = _primary(findings)
    return {
        "subject": {"kind": "run", "id": run["id"], "comp_id": run.get("comp_id"),
                    "action": run.get("action"), "state": run.get("state")},
        "severity": primary["severity"] if primary else "ok",
        "headline": _run_headline(run, stage, primary),
        "stage": stage,
        "cause": cause,
        "findings": findings,
        "facts": _run_facts(run, events, size, now),
        "log": {"path": run.get("log_path"), "bytes": size, "tail_from": from_offset},
        "as_of": now,
    }


def _run_facts(run, events, log_bytes, now):
    # A record created directly (a hand-built row, a test) may carry no run dir; a diagnosis
    # must still answer, so the workspace fields degrade to null rather than raising.
    key = ranges.range_key(run) if run.get("run_dir") else ""
    started = run.get("started_at")
    return {
        "returncode": run.get("returncode"),
        "created_at": run.get("created_at"),
        "started_at": started,
        "ended_at": run.get("ended_at"),
        "duration_s": ((run.get("ended_at") or now) - started) if started else None,
        "engine_ref": run.get("engine_ref"),
        "engine_head": run.get("engine_head"),
        "workspace": key or None,
        "workspace_missing": bool(key) and not Path(key).is_dir(),
        "log_bytes": log_bytes,
        "events": len(events),
        "last_activity_s": round(now - _last_activity(run, events), 1),
    }


def _run_brief(run):
    return {"id": run["id"], "action": run.get("action"), "state": run.get("state"),
            "stage": run.get("stage"), "returncode": run.get("returncode"),
            "created_at": run.get("created_at"), "error": redact(run.get("error") or "")}


def diagnose_range(store, rng, now=None):
    """A range's triage answer: its state plus every finding its runs and workspace carry."""
    now = time.time() if now is None else now
    key = rng["key"]
    runs = rng.get("runs") or []
    findings = []

    if rng.get("workspace_missing") and rng.get("state") != "retired":
        findings.append(_finding(
            "workspace_missing", "error", "The range's workspace is gone",
            "The estate document, its ownership proof and the teardown journal lived in that "
            "directory; without them the dashboard cannot prove which VMs it may destroy.",
            "Reclaim the VMs by tag on the nodes, then record the retirement with "
            "`POST /api/ranges/retire` so the dead range stops collecting attempts."))

    destroy = next((r for r in runs if r.get("action") == "destroy"), None)
    if rng.get("state") == "teardown_failed" or (
            destroy and destroy.get("state") in ("failed", "cancelled")
            and rng.get("state") != "retired"):
        cause = None
        if destroy:
            tail, offset, _, first_line = _log_tail(destroy)
            cause = find_cause(tail, offset, first_line)
        findings.append(_finding(
            "teardown_incomplete", "error", "Teardown did not complete",
            "A destroy exited non-zero, so resources may remain — the estate document's "
            "teardown section records which steps actually finished.",
            "Re-run teardown (it is idempotent and continues from the document). If the "
            "destroy keeps failing, read the quoted line and the teardown journal before "
            "tearing anything down by hand.",
            run_id=(destroy or {}).get("id"),
            evidence=_evidence(cause, destroy or {}, None)))

    # The newest non-destroy run is what a range-level "what's wrong" is really about.
    latest = next((r for r in runs
                   if r.get("action") not in ("destroy", "verify")), None)
    if latest and latest.get("state") in ("failed", "running", "queued", "paused", "pausing",
                                          "cancelled"):
        diag = diagnose_run(store, latest, now=now)
        for finding in diag["findings"]:
            findings.append({**finding, "run_id": latest["id"]})
        if latest.get("state") == "running" and not diag["findings"]:
            findings.append(_finding(
                "range_building", "info", "A run is building this range",
                f"{_ACTION_LABELS.get(latest['action'], latest['action'])} is in progress.",
                "Wait for it; a failed or stalled run will surface here with its cause.",
                run_id=latest["id"]))

    if rng.get("state") == "failed" and not findings:
        # No run explains it and nothing is recorded live: either the deploy never built a
        # range or its VMs were reclaimed outside the queue. Both leave the same question —
        # are tagged VMs still out there? — and only the nodes can answer it.
        findings.append(_finding(
            "range_unaccounted", "warning", "No live infrastructure is recorded for this range",
            "The newest run neither failed nor tore the range down, and the estate document "
            "records no VM of its own — a range built by hand or reclaimed outside the queue "
            "reads exactly like this.",
            "Check the nodes for this range's VMs/tags; if they are gone, retire the range "
            "with `POST /api/ranges/retire` so it stops resolving range actions."))

    kept = rng.get("kept_templates") or []
    if kept:
        kinds = sorted({str(row.get("kind") or "template") for row in kept})
        findings.append(_finding(
            "kept_templates", "info",
            f"{len(kept)} template VM{'s' if len(kept) != 1 else ''} kept on purpose",
            f"The teardown used `--teams-only`/`--keep-templates` and left {', '.join(kinds)} "
            "for this competition's next run.",
            "A full destroy reclaims them; the range page offers it while no live VMs remain."))

    flat = estate.load_flat(key)
    degradations = flat.get("degradations") or []
    if isinstance(degradations, list):
        for item in degradations[:5]:
            what = item.get("what") if isinstance(item, dict) else ""
            detail = item.get("detail") if isinstance(item, dict) else item
            findings.append(_finding(
                "degradation", "warning",
                f"Deploy degraded: {what}" if what else "The deploy reported a degradation",
                redact(detail) or "The driver recorded a degradation during the deploy.",
                "The range is usable but not what was asked for; check the quoted detail "
                "against the box it names before the competition starts."))

    if rng.get("state") in ("live", "building", "teardown_failed") and rng.get("has_recorded_vms"):
        from dashboard import scoring_cache
        snap = scoring_cache.snapshot(rng)
        if snap and snap.get("reachable") is False:
            findings.append(_finding(
                "engine_unreachable", "warning", "The range's engine does not answer",
                "The cached scoring poll could not log in or reach the engine's management "
                "address.",
                "Check the engine VM on its node and the management network; the board's "
                "scoring chip and the competition panel use the same observation."))

    pending = [req for req in (store.list_requests() if hasattr(store, "list_requests")
                               else [])
               if req.get("status") == "pending"
               and str(Path(req.get("range_key") or "").resolve()) == key]
    if pending:
        findings.append(_finding(
            "pending_requests", "info",
            f"{len(pending)} reset request{'s' if len(pending) != 1 else ''} awaiting review",
            "Teams asked the dashboard to reset a box; each request is approved or denied "
            "from the range's Teams tab.",
            "Approve submits a redeploy-reset run for that box; deny tells the team no."))

    primary = _primary(findings)
    state_word = {"building": "is building", "live": "is live", "retired": "is retired",
                  "teardown_failed": "has an incomplete teardown",
                  "failed": "is not deployed"}.get(rng.get("state"), rng.get("state"))
    head = f"{rng.get('comp_id')} range {state_word}"
    if primary and primary["severity"] in ("error", "warning"):
        head = f"{head}: {primary['title']}"
    cause = None
    for finding in findings:
        cause = next((e for e in finding.get("evidence") or []
                      if e.get("quote")), None)
        if cause:
            break
    return {
        "subject": {"kind": "range", "id": rng.get("id"), "key": key,
                    "comp_id": rng.get("comp_id"), "state": rng.get("state")},
        "severity": primary["severity"] if primary else "ok",
        "headline": head,
        "cause": cause,
        "findings": findings,
        "facts": {
            "state": rng.get("state"),
            "live": rng.get("live"),
            "has_recorded_vms": rng.get("has_recorded_vms"),
            "kept_templates": len(kept),
            "workspace": key,
            "workspace_missing": rng.get("workspace_missing"),
            "engine_ip": flat.get("engine_mgmt_ip"),
            "engine_ref": rng.get("engine_ref"),
            "engine_head": rng.get("engine_head"),
            "deploy_run_id": rng.get("deploy_run_id"),
            "teams": rng.get("teams"),
            "last_phase": flat.get("last_phase"),
            "pending_requests": len(pending),
        },
        "runs": [_run_brief(r) for r in runs[:8]],
        "as_of": now,
    }
