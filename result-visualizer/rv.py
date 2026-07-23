#!/usr/bin/env python3
"""Result Visualizer (rv) — an interactive browser for tau2-bench results.json files.

Why this exists: results.json files can be tens of MB and choke editors. This tool
never dumps the whole thing. It lists runs straight from folder names (instant, no
parsing), loads a single file's JSON only when you open it, and lets you page through
simulations and messages one step at a time.

Usage:
    python rv.py                 # browse the default leaderboard dir
    python rv.py <dir>           # browse a different dir (recursively finds results.json)
    python rv.py <results.json>  # open one file directly
    python rv.py --list          # non-interactive: print the index and exit
"""

import json
import os
import shutil
import sys
import textwrap

# Default place to look for results.json files. Override with a CLI arg.
DEFAULT_ROOT = (
    "/Users/ddod/LEANMCP/MODEL_TRAINING/tau2-bench/data/simulations/leaderboard"
)
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".rv_cache.json")
SECRET_KEYS = {
    "api_key",
    "apikey",
    "key",
    "token",
    "authorization",
    "secret",
    "password",
}


# ---------------------------------------------------------------------------
# Small presentation helpers
# ---------------------------------------------------------------------------
_TTY = sys.stdout.isatty()


def c(text, code):
    """Wrap text in an ANSI color if we're on a terminal."""
    if not _TTY:
        return str(text)
    return f"\033[{code}m{text}\033[0m"


def bold(t):
    return c(t, "1")


def dim(t):
    return c(t, "2")


def width():
    return shutil.get_terminal_size((100, 24)).columns


def human(nbytes):
    for unit in ("B", "K", "M", "G"):
        if nbytes < 1024 or unit == "G":
            return f"{nbytes:.0f}{unit}" if unit == "B" else f"{nbytes:.1f}{unit}"
        nbytes /= 1024


def reward_color(r):
    if r is None:
        return dim("  ?  ")
    if r >= 0.999:
        return c(f"{r:.2f}", "32")  # green
    if r <= 0.001:
        return c(f"{r:.2f}", "31")  # red
    return c(f"{r:.2f}", "33")  # yellow


def wrap(text, indent="    "):
    out = []
    for line in str(text).splitlines() or [""]:
        out.extend(
            textwrap.wrap(
                line,
                width=max(40, width() - len(indent)),
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    return "\n".join(indent + ln for ln in out)


def hr(char="-"):
    print(dim(char * min(width(), 100)))


def redact(obj):
    """Recursively mask values whose key looks like a secret."""
    if isinstance(obj, dict):
        return {
            k: ("***redacted***" if k.lower() in SECRET_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Summary cache (so the index can show sim-count / avg-reward without reloading)
# ---------------------------------------------------------------------------
def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Discovery — build the index from the filesystem (no JSON parsing)
# ---------------------------------------------------------------------------
def discover(root):
    """Find results.json files and derive run/model/domain from the path names."""
    entries = []
    if os.path.isfile(root):
        files = [root]
    else:
        files = []
        for dirpath, _dirs, names in os.walk(root):
            if "results.json" in names:
                files.append(os.path.join(dirpath, "results.json"))
    for path in sorted(files):
        leaf = os.path.basename(
            os.path.dirname(path)
        )  # e.g. glm-5p2__banking_knowledge
        run = os.path.basename(
            os.path.dirname(os.path.dirname(path))
        )  # e.g. run_2026..._210123
        if "__" in leaf:
            model, domain = leaf.split("__", 1)
        else:
            model, domain = leaf, ""
        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            size, mtime = 0, 0
        entries.append(
            {
                "path": path,
                "run": run,
                "model": model,
                "domain": domain,
                "size": size,
                "mtime": mtime,
            }
        )
    return entries


def print_index(entries, cache):
    hr("=")
    print(bold("  RESULT VISUALIZER") + dim("   —   tau2-bench results browser"))
    hr("=")
    if not entries:
        print("  (no results.json files found)")
        return
    header = f"  {'#':>2}  {'run':<22} {'model':<22} {'domain':<18} {'size':>6} {'sims':>5} {'avg':>6}"
    print(bold(header))
    hr()
    for i, e in enumerate(entries):
        info = cache.get(e["path"])
        fresh = (
            info and info.get("mtime") == e["mtime"] and info.get("size") == e["size"]
        )
        nsims = str(info["n_sims"]) if fresh else dim("—")
        avg = reward_color(info["avg_reward"]) if fresh else dim("—")
        print(
            f"  {i:>2}  {e['run']:<22} {e['model']:<22} {e['domain']:<18} "
            f"{human(e['size']):>6} {nsims:>5} {avg:>6}"
        )
    hr()
    print(
        dim(
            "  enter a number to open  ·  'scan' to compute all summaries  ·  'q' to quit"
        )
    )


# ---------------------------------------------------------------------------
# Loading + summarizing a single result file
# ---------------------------------------------------------------------------
def load_result(entry):
    size = human(entry["size"])
    sys.stdout.write(dim(f"  loading {os.path.basename(entry['path'])} ({size})… "))
    sys.stdout.flush()
    with open(entry["path"]) as f:
        data = json.load(f)
    print(dim("done."))
    return data


def _reward_of(sim):
    """Reward of a sim, tolerating reward_info being absent or null."""
    return (sim.get("reward_info") or {}).get("reward")


def summarize(data):
    sims = data.get("simulations", [])
    rewards = [_reward_of(s) for s in sims]
    rewards = [r for r in rewards if isinstance(r, (int, float))]
    avg = sum(rewards) / len(rewards) if rewards else None
    return {"n_sims": len(sims), "avg_reward": avg}


def update_cache_for(entry, data, cache):
    s = summarize(data)
    cache[entry["path"]] = {
        "mtime": entry["mtime"],
        "size": entry["size"],
        "n_sims": s["n_sims"],
        "avg_reward": s["avg_reward"],
    }
    save_cache(cache)
    return s


def print_config(data):
    info = data.get("info", {})
    agent = info.get("agent_info", {}) or {}
    user = info.get("user_info", {}) or {}
    env = info.get("environment_info", {}) or {}
    hr("=")
    print(bold("  RUN CONFIG"))
    hr()
    print(f"  timestamp     {data.get('timestamp', '?')}")
    print(f"  domain        {env.get('domain_name', '?')}")
    print(f"  agent llm     {agent.get('llm', '?')}")
    print(f"  user  llm     {user.get('llm', '?')}")
    print(f"  num_trials    {info.get('num_trials', '?')}")
    print(
        f"  max_steps     {info.get('max_steps', '?')}    max_errors {info.get('max_errors', '?')}"
    )
    print(f"  git_commit    {info.get('git_commit', '?')}")
    print(dim("  (api keys redacted; use 'cfg' for the full config)"))


def print_full_config(data):
    print(json.dumps(redact(data.get("info", {})), indent=2)[:8000])


def print_sim_list(data):
    sims = data.get("simulations", [])
    hr("=")
    s = summarize(data)
    avg = reward_color(s["avg_reward"])
    print(bold(f"  SIMULATIONS  ({s['n_sims']} total, avg reward {avg})"))
    hr()
    print(
        bold(
            f"  {'#':>3}  {'task_id':<28} {'trial':>5} {'reward':>6} {'msgs':>5}  termination"
        )
    )
    hr()
    for i, sim in enumerate(sims):
        r = _reward_of(sim)
        tid = str(sim.get("task_id", "?"))[:28]
        term = str(sim.get("termination_reason", ""))
        nmsg = len(sim.get("messages", []))
        print(
            f"  {i:>3}  {tid:<28} {str(sim.get('trial', '')):>5} "
            f"{reward_color(r):>6} {nmsg:>5}  {term}"
        )
    hr()
    print(
        dim(
            "  number = open simulation  ·  'cfg' full config  ·  'b' back  ·  'q' quit"
        )
    )


# ---------------------------------------------------------------------------
# Message rendering + per-simulation browsing
# ---------------------------------------------------------------------------
def render_message(msg, idx, total):
    role = msg.get("role", "?")
    role_code = {"assistant": "36", "user": "35", "tool": "33", "system": "34"}.get(
        role, "0"
    )
    hr()
    head = f"  [{idx + 1}/{total}]  " + c(role.upper(), role_code + ";1")
    turn = msg.get("turn_idx")
    if turn is not None:
        head += dim(f"   turn={turn}")
    usage = msg.get("usage") or {}
    if usage:
        head += dim(
            f"   tok p={usage.get('prompt_tokens', '?')}/c={usage.get('completion_tokens', '?')}"
        )
    if msg.get("cost"):
        head += dim(f"   ${msg.get('cost')}")
    print(head)

    # reasoning, if the provider exposed it
    reasoning = (
        ((msg.get("raw_data") or {}).get("choices") or [{}])[0]
        .get("message", {})
        .get("reasoning_content")
    )
    if reasoning:
        print(dim("  reasoning:"))
        print(dim(wrap(reasoning)))

    content = msg.get("content")
    if content:
        if len(str(content)) > 3000:
            print(wrap(str(content)[:3000]))
            print(
                dim(f"    … (+{len(str(content)) - 3000} chars — press 'f' for full)")
            )
        else:
            print(wrap(content))
    elif not msg.get("tool_calls"):
        print(dim("    (no content)"))

    for tc in msg.get("tool_calls") or []:
        name = tc.get("name") or (tc.get("function") or {}).get("name", "?")
        args = tc.get("arguments")
        if args is None:
            args = (tc.get("function") or {}).get("arguments")
        print(c(f"    → {name}(", "32") + c(f"{args}", "32") + c(")", "32"))


def browse_sim(data, sim_idx):
    sims = data.get("simulations", [])
    sim = sims[sim_idx]
    msgs = sim.get("messages", [])
    tasks = {t.get("id"): t for t in data.get("tasks", [])}
    task = tasks.get(sim.get("task_id"))

    hr("=")
    r = _reward_of(sim)
    print(
        bold(
            f"  SIM #{sim_idx}  task={sim.get('task_id')}  reward={reward_color(r)}  "
            f"term={sim.get('termination_reason')}"
        )
    )
    print(
        dim(
            f"  {len(msgs)} messages   duration={sim.get('duration')}   trial={sim.get('trial')}"
        )
    )
    hr()
    print(
        dim(
            "  ENTER/n next · p prev · <num> jump · f full content · t task · "
            "r reward · raw · a all · b back · q quit"
        )
    )

    i = 0
    if msgs:
        render_message(msgs[i], i, len(msgs))
    while True:
        try:
            cmd = input(c("\n  msg> ", "1")).strip()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if cmd in ("q", "quit"):
            return "quit"
        if cmd in ("b", "back", ""):
            if cmd == "":  # ENTER = next
                if i < len(msgs) - 1:
                    i += 1
                    render_message(msgs[i], i, len(msgs))
                else:
                    print(dim("  (end of conversation)"))
                continue
            return "back"
        if cmd in ("n", "next"):
            if i < len(msgs) - 1:
                i += 1
                render_message(msgs[i], i, len(msgs))
            else:
                print(dim("  (end of conversation)"))
        elif cmd in ("p", "prev"):
            if i > 0:
                i -= 1
                render_message(msgs[i], i, len(msgs))
            else:
                print(dim("  (start of conversation)"))
        elif cmd == "f":
            print(wrap(str(msgs[i].get("content", ""))))
        elif cmd == "raw":
            print(json.dumps(redact(msgs[i]), indent=2, default=str)[:6000])
        elif cmd == "t":
            if task:
                print(bold("  TASK"))
                print(f"  id: {task.get('id')}")
                print(wrap(task.get("description", "")))
                ec = task.get("evaluation_criteria")
                if ec:
                    print(bold("\n  evaluation_criteria:"))
                    print(wrap(json.dumps(ec, indent=2)))
            else:
                print(dim("  (task definition not found)"))
        elif cmd == "r":
            print(bold("  REWARD_INFO"))
            print(
                wrap(
                    json.dumps(sim.get("reward_info", {}), indent=2, default=str)[:6000]
                )
            )
        elif cmd in ("a", "all"):
            for k, m in enumerate(msgs):
                render_message(m, k, len(msgs))
        elif cmd.isdigit():
            j = int(cmd)
            if 0 <= j < len(msgs):
                i = j
                render_message(msgs[i], i, len(msgs))
            else:
                print(dim(f"  (out of range 0..{len(msgs) - 1})"))
        else:
            print(dim("  ? unknown command"))


# ---------------------------------------------------------------------------
# Top-level interactive loops
# ---------------------------------------------------------------------------
def result_loop(entry, data):
    print_config(data)
    print_sim_list(data)
    while True:
        try:
            cmd = input(c("\n  sim> ", "1")).strip()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if cmd in ("q", "quit"):
            return "quit"
        if cmd in ("b", "back", ""):
            return "back"
        if cmd in ("cfg", "config"):
            print_full_config(data)
        elif cmd in ("l", "ls", "list"):
            print_sim_list(data)
        elif cmd.isdigit():
            j = int(cmd)
            if 0 <= j < len(data.get("simulations", [])):
                if browse_sim(data, j) == "quit":
                    return "quit"
                print_sim_list(data)
            else:
                print(dim("  (out of range)"))
        else:
            print(dim("  ? unknown command — number, 'cfg', 'b', or 'q'"))


def main():
    args = [a for a in sys.argv[1:]]
    list_only = "--list" in args
    args = [a for a in args if a != "--list"]
    root = args[0] if args else DEFAULT_ROOT

    if not os.path.exists(root):
        print(f"path not found: {root}")
        sys.exit(1)

    cache = load_cache()
    entries = discover(root)

    if list_only:
        print_index(entries, cache)
        return

    while True:
        print_index(entries, cache)
        try:
            cmd = input(c("\n  rv> ", "1")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if cmd in ("q", "quit"):
            return
        if cmd in ("scan", "scanall"):
            for e in entries:
                try:
                    data = load_result(e)
                    update_cache_for(e, data, cache)
                except (OSError, ValueError) as ex:
                    print(dim(f"  skip {e['path']}: {ex}"))
            continue
        if cmd.isdigit():
            j = int(cmd)
            if 0 <= j < len(entries):
                try:
                    data = load_result(entries[j])
                except (OSError, ValueError) as ex:
                    print(f"  failed to load: {ex}")
                    continue
                update_cache_for(entries[j], data, cache)
                if result_loop(entries[j], data) == "quit":
                    return
            else:
                print(dim("  (out of range)"))
        else:
            print(dim("  ? enter a number, 'scan', or 'q'"))


if __name__ == "__main__":
    main()
