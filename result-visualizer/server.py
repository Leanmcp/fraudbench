#!/usr/bin/env python3
"""Web version of the Result Visualizer.

A tiny stdlib HTTP server (no Flask, no pip install) that reuses the parsing logic
from rv.py and serves a minimal HTML/JS front-end. Same laziness as the CLI:
the index is built from folder names, a file's JSON is parsed only when opened,
and parsed files are cached in memory for the life of the process.

Usage:
    python server.py                 # serve the default leaderboard dir on :8765
    python server.py <dir>           # serve a different dir
    python server.py --port 9000     # pick a port

Then open http://localhost:8765 in your browser.

Addressing is index-based (result index `r`, simulation index `s`) so the browser
never sends filesystem paths — no path-traversal surface.
"""

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import rv  # reuse discover / summarize / redact / cache helpers

HERE = os.path.dirname(os.path.abspath(__file__))
# Where the domain knowledge-base documents live (the card specs etc. a task's
# required_documents point to). Sits alongside this repo's data/ tree.
DOMAINS_DIR = os.path.normpath(os.path.join(HERE, "..", "data", "tau2", "domains"))

# Populated in main()
ENTRIES = []  # list from rv.discover()
SUMMARY_CACHE = {}  # persisted summary cache (rv cache file)
LOADED = {}  # path -> parsed JSON, in-memory for the process lifetime


def load_required_docs(domain, task):
    """Load the full KB documents a task's evaluation depends on (its 'why').

    Tasks reference documents by id in `required_documents`; the content lives
    in data/tau2/domains/<domain>/documents/<id>.json. Returns a list of dicts
    with whatever each file holds (typically id/title/content).
    """
    ids = (task or {}).get("required_documents") or []
    if not domain or not ids:
        return []
    ddir = os.path.join(DOMAINS_DIR, domain, "documents")
    out = []
    for did in ids:
        path = os.path.join(ddir, f"{did}.json")
        try:
            with open(path) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            out.append(
                {"id": did, "title": None, "content": f"(document not found at {path})"}
            )
    return out


# Per-domain human-written task write-ups (the "why", traps, source docs).
EXPLANATION_FILES = {
    "banking_knowledge": os.path.normpath(
        os.path.join(
            HERE,
            "..",
            "CODE_EXPLANATION",
            "BANKING_DATA_EXPLANATION",
            "TASKS_EXPLANATION.md",
        )
    ),
}
_EXPLANATION_CACHE = {}  # path -> {task_id: markdown section}


def _parse_explanations(path):
    """Split a TASKS_EXPLANATION.md into {task_id: section} once, then cache.

    Each task is a markdown section starting at a heading like
    `## task_001 — ...` and running until the next such heading.
    """
    if path in _EXPLANATION_CACHE:
        return _EXPLANATION_CACHE[path]
    sections = {}
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        _EXPLANATION_CACHE[path] = sections
        return sections
    # re.split keeps the headings (one capture group) interleaved with bodies.
    parts = re.split(r"(?m)^(##\s+task_\d+\b.*)$", text)
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        m = re.match(r"##\s+(task_\d+)", heading)
        if not m:
            continue
        sec = heading + "\n" + body
        # trim trailing rules / "<!-- BATCH .. -->" markers bled in from the gap
        sec = re.sub(r"(\s*(?:---|<!--\s*BATCH.*?-->)\s*)+$", "", sec, flags=re.S)
        sections[m.group(1)] = sec.strip()
    _EXPLANATION_CACHE[path] = sections
    return sections


def load_task_explanation(domain, task_id):
    path = EXPLANATION_FILES.get(domain)
    if not path or task_id is None:
        return None
    return _parse_explanations(path).get(str(task_id))


# Difficulty tags produced by ../sort-tasks (task_difficulty.csv: task_id -> bucket).
DIFFICULTY_FILES = {
    "banking_knowledge": os.path.normpath(
        os.path.join(HERE, "..", "sort-tasks", "task_difficulty.csv")
    ),
}
_DIFFICULTY_CACHE = {}  # path -> {task_id: bucket}


def _load_difficulty_map(path):
    if path in _DIFFICULTY_CACHE:
        return _DIFFICULTY_CACHE[path]
    out = {}
    try:
        import csv

        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                tid, bucket = row.get("task_id"), row.get("bucket")
                if tid and bucket:
                    out[tid] = bucket
    except (OSError, ValueError):
        pass
    _DIFFICULTY_CACHE[path] = out
    return out


def task_difficulty(domain, task_id):
    path = DIFFICULTY_FILES.get(domain)
    if not path or task_id is None:
        return None
    return _load_difficulty_map(path).get(str(task_id))


# ---------------------------------------------------------------------------
# Data access (mirrors rv.py, returns JSON-able dicts instead of printing)
# ---------------------------------------------------------------------------
def get_loaded(entry):
    """Parse a result file once, then serve it from memory."""
    path = entry["path"]
    if path not in LOADED:
        with open(path) as f:
            LOADED[path] = json.load(f)
        rv.update_cache_for(entry, LOADED[path], SUMMARY_CACHE)
    return LOADED[path]


def index_payload():
    out = []
    for i, e in enumerate(ENTRIES):
        info = SUMMARY_CACHE.get(e["path"])
        fresh = (
            info and info.get("mtime") == e["mtime"] and info.get("size") == e["size"]
        )
        out.append(
            {
                "i": i,
                "run": e["run"],
                "model": e["model"],
                "domain": e["domain"],
                "size": rv.human(e["size"]),
                "bytes": e["size"],
                "n_sims": info["n_sims"] if fresh else None,
                "avg_reward": info["avg_reward"] if fresh else None,
            }
        )
    return out


def result_payload(ri):
    entry = ENTRIES[ri]
    data = get_loaded(entry)
    info = data.get("info", {}) or {}
    env = info.get("environment_info", {}) or {}
    agent = info.get("agent_info", {}) or {}
    user = info.get("user_info", {}) or {}
    domain = env.get("domain_name")
    sims = data.get("simulations", []) or []
    sim_rows = []
    for si, s in enumerate(sims):
        sim_rows.append(
            {
                "s": si,
                "task_id": s.get("task_id"),
                "trial": s.get("trial"),
                "reward": rv._reward_of(s),
                "n_msgs": len(s.get("messages", []) or []),
                "termination": s.get("termination_reason"),
                "duration": s.get("duration"),
                "difficulty": task_difficulty(domain, s.get("task_id")),
            }
        )
    summ = rv.summarize(data)
    return {
        "config": {
            "timestamp": data.get("timestamp"),
            "domain": env.get("domain_name"),
            "agent_llm": agent.get("llm"),
            "user_llm": user.get("llm"),
            "num_trials": info.get("num_trials"),
            "max_steps": info.get("max_steps"),
            "max_errors": info.get("max_errors"),
            "git_commit": info.get("git_commit"),
            "full": rv.redact(info),
        },
        "summary": summ,
        "sims": sim_rows,
    }


def _view_message(m):
    # Thinking/reasoning can live inside the raw API response or, for some
    # providers, directly on the stored message. Check both, for agent + user.
    raw_msg = ((m.get("raw_data") or {}).get("choices") or [{}])[0].get(
        "message", {}
    ) or {}
    reasoning = (
        raw_msg.get("reasoning_content")
        or raw_msg.get("reasoning")
        or m.get("reasoning_content")
        or m.get("reasoning")
    )
    tcs = []
    for tc in m.get("tool_calls") or []:
        name = tc.get("name") or (tc.get("function") or {}).get("name")
        args = tc.get("arguments")
        if args is None:
            args = (tc.get("function") or {}).get("arguments")
        tcs.append({"name": name, "args": args})
    return {
        "role": m.get("role"),
        "content": m.get("content"),
        "reasoning": reasoning,
        "tool_calls": tcs,
        "turn_idx": m.get("turn_idx"),
        "usage": m.get("usage"),
        "cost": m.get("cost"),
    }


def sim_payload(ri, si):
    data = get_loaded(ENTRIES[ri])
    info = data.get("info", {}) or {}
    env = info.get("environment_info", {}) or {}
    agent = info.get("agent_info", {}) or {}
    user = info.get("user_info", {}) or {}
    sim = (data.get("simulations", []) or [])[si]
    tasks = {t.get("id"): t for t in (data.get("tasks", []) or [])}
    task = tasks.get(sim.get("task_id"))
    task_view = None
    if task:
        task_view = {
            "id": task.get("id"),
            "description": task.get("description"),
            "evaluation_criteria": task.get("evaluation_criteria"),
            "user_scenario": task.get("user_scenario"),
            "required_documents": task.get("required_documents"),
        }
    return {
        "s": si,
        "task_id": sim.get("task_id"),
        "trial": sim.get("trial"),
        "reward": rv._reward_of(sim),
        "termination": sim.get("termination_reason"),
        "duration": sim.get("duration"),
        "reward_info": rv.redact(sim.get("reward_info")),
        "difficulty": task_difficulty(env.get("domain_name"), sim.get("task_id")),
        "task": task_view,
        # Full KB documents the task's gold answer depends on (the "why").
        "documents": load_required_docs(env.get("domain_name"), task),
        # Human-written write-up for this task (parsed from TASKS_EXPLANATION.md).
        "task_explanation": load_task_explanation(
            env.get("domain_name"), sim.get("task_id")
        ),
        # Run-level system prompts (same for every sim in the run) so the UI can
        # show the agent's policy and the user-simulator guidelines side by side.
        "prompts": {
            "agent_llm": agent.get("llm"),
            "user_llm": user.get("llm"),
            "agent_system_prompt": env.get("policy"),
            "user_system_prompt": user.get("global_simulation_guidelines"),
        },
        "messages": [_view_message(m) for m in (sim.get("messages", []) or [])],
    }


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass  # quiet

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        def qint(name):
            try:
                return int(q.get(name, [None])[0])
            except (TypeError, ValueError):
                return None

        try:
            if u.path in ("/", "/index.html"):
                return self._file("index.html", "text/html; charset=utf-8")
            if u.path in ("/compare", "/compare.html"):
                return self._file("compare.html", "text/html; charset=utf-8")
            if u.path == "/api/results":
                return self._json(index_payload())
            if u.path == "/api/result":
                ri = qint("r")
                if ri is None or not (0 <= ri < len(ENTRIES)):
                    return self._json({"error": "bad result index"}, 400)
                return self._json(result_payload(ri))
            if u.path == "/api/sim":
                ri = qint("r")
                if ri is None or not (0 <= ri < len(ENTRIES)):
                    return self._json({"error": "bad result index"}, 400)
                data = get_loaded(ENTRIES[ri])
                sims = data.get("simulations", []) or []
                si = qint("s")
                if si is None:  # fall back to lookup by task_id (used by compare page)
                    tid = q.get("task_id", [None])[0]
                    si = next(
                        (
                            k
                            for k, s in enumerate(sims)
                            if str(s.get("task_id")) == str(tid)
                        ),
                        None,
                    )
                if si is None or not (0 <= si < len(sims)):
                    return self._json({"error": "sim not found"}, 404)
                return self._json(sim_payload(ri, si))
            self.send_error(404)
        except Exception as ex:  # never let the server die on one bad request
            self._json({"error": f"{type(ex).__name__}: {ex}"}, 500)


def main():
    args = sys.argv[1:]
    port = 8765
    if "--port" in args:
        i = args.index("--port")
        port = int(args[i + 1])
        del args[i : i + 2]
    root = args[0] if args else rv.DEFAULT_ROOT

    global ENTRIES, SUMMARY_CACHE
    SUMMARY_CACHE = rv.load_cache()
    ENTRIES = rv.discover(root)

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Result Visualizer web UI  →  http://localhost:{port}")
    print(f"  serving {len(ENTRIES)} result file(s) from {root}")
    print("  Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
