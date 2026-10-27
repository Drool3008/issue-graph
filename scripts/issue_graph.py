#!/usr/bin/env python3
"""issue-graph: read-only GitHub issue dependency snapshots, diffs and views.

Subcommands
  sync    Fetch every issue in a repo through `gh api graphql` (read-only),
          compare with the previous snapshot, store the new snapshot and print
          a compact digest (JSON) for Claude to reason over.
  render  Build the self-contained HTML graph/schedule view from the latest
          snapshot plus Claude's notes, and optionally open it.

Only the Python standard library and the GitHub CLI (`gh`) are required.
The script never sends a GraphQL mutation or a non-GET REST call.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = PLUGIN_ROOT / "assets" / "template.html"
CYTOSCAPE = PLUGIN_ROOT / "assets" / "vendor" / "cytoscape.min.js"

PAGE_SIZE = 100
# Closed issues are fetched only if updated within this window (or since the
# previous sync, if that is older), so big repos sync in about a minute.
CLOSED_WINDOW_DAYS = 84  # matches the 12-week created-vs-closed chart

ISSUE_FIELDS_CORE = """
        number title url state stateReason createdAt updatedAt closedAt body
        author { login }
        assignees(first: 10) { nodes { login } }
        labels(first: 20) { nodes { name color } }
        milestone { title dueOn }
        comments { totalCount }
        closedByPullRequestsReferences(first: 10, includeClosedPrs: true) {
          nodes {
            number title url state isDraft merged reviewDecision
            author { login }
            reviewRequests(first: 10) {
              nodes { requestedReviewer {
                ... on User { login } ... on Team { name }
              } }
            }
            latestReviews(first: 10) { nodes { author { login } state } }
          }
        }
"""

ISSUE_FIELDS_DEPS = """
        parent { number }
        blockedBy(first: 50) { nodes { number repository { nameWithOwner } } }
        blocking(first: 50) { nodes { number repository { nameWithOwner } } }
"""

QUERY_TEMPLATE = """
query($owner: String!, $name: String!, $cursor: String) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    isPrivate
    url
    issues(first: %d, after: $cursor, %s, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
%s
      }
    }
  }
}
"""

# Phrases in an issue body that hint at an undeclared dependency.
TEXT_BLOCKED_BY = re.compile(
    r"(?i)\b(blocked\s+by|depends\s+on|depends\s+upon|dependent\s+on|waiting\s+(?:on|for)|"
    r"prerequisites?:?|needs)\s+((?:#\d+(?:\s*(?:,|and|&)\s*)?)+)"
)
TEXT_BLOCKS = re.compile(r"(?i)\b(blocks|blocking|is\s+a\s+blocker\s+for)\s+((?:#\d+(?:\s*(?:,|and|&)\s*)?)+)")
ISSUE_REF = re.compile(r"#(\d+)")

PRIORITY_HIGH = re.compile(r"(?i)\b(p0|critical|urgent|blocker|sev[- ]?1|priority[: -]?(high|critical))\b")
PRIORITY_MED = re.compile(r"(?i)\b(p1|high|important|sev[- ]?2)\b")
BUG = re.compile(r"(?i)\bbug\b")


class SyncError(Exception):
    pass


# --------------------------------------------------------------------------- gh

def run_gh(args: list[str]) -> str:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise SyncError("The GitHub CLI (gh) is not installed. Install it from https://cli.github.com and run `gh auth login`.")
    except subprocess.TimeoutExpired:
        raise SyncError("The GitHub request timed out. Try again.")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        low = err.lower()
        if "gh auth login" in low or "not logged" in low or "authentication" in low or "http 401" in low:
            raise SyncError("gh is not authenticated. Run `gh auth login` (private repos need the `repo` scope).\n" + err)
        raise SyncError(err or "gh exited with an error")
    return proc.stdout


def graphql(query: str, variables: dict) -> dict:
    # Read-only guard: this plugin must never change anything on GitHub.
    stripped = query.lstrip()
    if not stripped.startswith("query") or re.search(r"\bmutation\b", query):
        raise SyncError("Refusing to send a non-read GraphQL operation.")
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is not None:
            args += ["-f", f"{key}={value}"]
    out = run_gh(args)
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        raise SyncError("GitHub returned something that is not JSON:\n" + out[:500])
    if payload.get("errors"):
        messages = "; ".join(e.get("message", str(e)) for e in payload["errors"])
        raise SyncError(messages)
    return payload["data"]


def resolve_repo(repo: str | None) -> str:
    if repo:
        repo = repo.strip()
        m = re.match(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", repo)
        if not m:
            raise SyncError(f"'{repo}' is not an owner/repo name or GitHub URL.")
        return f"{m.group(1)}/{m.group(2)}"
    try:
        out = run_gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).strip()
    except SyncError as e:
        raise SyncError("No repo given and the current folder is not a GitHub repo. Pass owner/repo.\n" + str(e))
    return out


def fetch_issues(repo: str, issue_filter: str, fields: str, label: str) -> tuple[dict, list[dict]]:
    owner, name = repo.split("/", 1)
    query = QUERY_TEMPLATE % (PAGE_SIZE, issue_filter, fields)
    nodes: list[dict] = []
    cursor = None
    meta = None
    while True:
        try:
            data = graphql(query, {"owner": owner, "name": name, "cursor": cursor})
        except SyncError as e:
            if "Could not resolve to a Repository" in str(e):
                raise SyncError(f"Repository {repo} was not found, or your gh account cannot read it.")
            raise
        repository = data.get("repository")
        if repository is None:
            raise SyncError(f"Repository {repo} was not found, or your gh account cannot read it.")
        conn = repository["issues"]
        if meta is None:
            meta = {
                "repo": repository["nameWithOwner"],
                "url": repository["url"],
                "private": repository["isPrivate"],
                "viewer": (data.get("viewer") or {}).get("login"),
                "total_reported": conn["totalCount"],
            }
        nodes.extend(n for n in conn["nodes"] if n)
        # Progress goes to stderr so stdout stays a single JSON digest.
        print(f"{label}: {len(nodes)}/{meta['total_reported']}", file=sys.stderr, flush=True)
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return meta, nodes


def fetch_all(repo: str, closed_since: dt.datetime) -> tuple[dict, list[dict], bool]:
    """All open issues, plus closed issues updated since `closed_since`."""
    since = closed_since.isoformat().replace("+00:00", "Z")
    filters = [("states: OPEN", "open issues"),
               (f'states: CLOSED, filterBy: {{since: "{since}"}}', "recently closed issues")]
    for deps_supported in (True, False):
        fields = ISSUE_FIELDS_CORE + (ISSUE_FIELDS_DEPS if deps_supported else "")
        try:
            meta, nodes = fetch_issues(repo, filters[0][0], fields, filters[0][1])
            _, closed = fetch_issues(repo, filters[1][0], fields, filters[1][1])
        except SyncError as e:
            msg = str(e)
            if deps_supported and re.search(r"blockedBy|blocking|parent", msg) and "doesn't exist" in msg:
                # Older GitHub Enterprise Server without issue dependencies.
                continue
            raise
        return meta, nodes + closed, deps_supported
    raise SyncError("Could not fetch issues.")


# --------------------------------------------------------------------- normalize

def _logins(conn) -> list[str]:
    return [n["login"] for n in ((conn or {}).get("nodes") or []) if n and n.get("login")]


def _snippet(text: str, start: int, end: int) -> str:
    a, b = max(0, start - 50), min(len(text), end + 50)
    s = " ".join(text[a:b].split())
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


def text_refs(body: str, number: int) -> list[dict]:
    """Dependency hints written in prose, e.g. 'blocked by #12'."""
    refs = []
    if not body:
        return refs
    # Drop fenced code blocks and quoted replies, where references are noise.
    clean = re.sub(r"```.*?```", " ", body, flags=re.S)
    clean = "\n".join(l for l in clean.splitlines() if not l.lstrip().startswith(">"))
    for m in TEXT_BLOCKED_BY.finditer(clean):
        for ref in ISSUE_REF.findall(m.group(2)):
            refs.append({"blocker": int(ref), "blocked": number, "snippet": _snippet(clean, m.start(), m.end())})
    for m in TEXT_BLOCKS.finditer(clean):
        for ref in ISSUE_REF.findall(m.group(2)):
            refs.append({"blocker": number, "blocked": int(ref), "snippet": _snippet(clean, m.start(), m.end())})
    return [r for r in refs if r["blocker"] != r["blocked"]]


def normalize(meta: dict, raw_nodes: list[dict]) -> dict:
    repo = meta["repo"].lower()
    issues = {}
    edges = set()
    hints = []
    for n in raw_nodes:
        num = n["number"]
        prs = []
        for p in ((n.get("closedByPullRequestsReferences") or {}).get("nodes") or []):
            if not p:
                continue
            requested = []
            for rr in ((p.get("reviewRequests") or {}).get("nodes") or []):
                who = (rr or {}).get("requestedReviewer") or {}
                if who.get("login"):
                    requested.append(who["login"])
                elif who.get("name"):
                    requested.append("team:" + who["name"])
            reviews = [
                {"who": (r.get("author") or {}).get("login") or "ghost", "state": r.get("state")}
                for r in ((p.get("latestReviews") or {}).get("nodes") or []) if r
            ]
            prs.append({
                "n": p["number"], "title": p["title"], "url": p["url"],
                "state": "merged" if p.get("merged") else p["state"].lower(),
                "draft": bool(p.get("isDraft")), "decision": p.get("reviewDecision"),
                "author": (p.get("author") or {}).get("login"),
                "requested": requested, "reviews": reviews,
            })
        blocked_by = sorted({
            b["number"] for b in ((n.get("blockedBy") or {}).get("nodes") or [])
            if b and (b.get("repository") or {}).get("nameWithOwner", "").lower() == repo
        })
        blocking = sorted({
            b["number"] for b in ((n.get("blocking") or {}).get("nodes") or [])
            if b and (b.get("repository") or {}).get("nameWithOwner", "").lower() == repo
        })
        for b in blocked_by:
            edges.add((b, num))
        for b in blocking:
            edges.add((num, b))
        hints.extend(text_refs(n.get("body") or "", num))
        issues[num] = {
            "n": num,
            "title": n["title"],
            "url": n["url"],
            "state": n["state"].lower(),
            "reason": (n.get("stateReason") or "").lower() or None,
            "created": n["createdAt"],
            "updated": n["updatedAt"],
            "closed": n.get("closedAt"),
            "author": (n.get("author") or {}).get("login") or "ghost",
            "assignees": _logins(n.get("assignees")),
            "labels": [{"name": l["name"], "color": l.get("color")} for l in ((n.get("labels") or {}).get("nodes") or []) if l],
            "milestone": (n.get("milestone") or {}).get("title"),
            "comments": (n.get("comments") or {}).get("totalCount", 0),
            "parent": (n.get("parent") or {}).get("number"),
            "prs": prs,
        }
    edges = {e for e in edges if e[0] in issues and e[1] in issues}
    for i in issues.values():
        i["blocked_by"] = sorted(a for a, b in edges if b == i["n"])
        i["blocking"] = sorted(b for a, b in edges if a == i["n"])
    # Keep only hints that point at real issues and are not already declared.
    seen = set()
    suggestions = []
    for h in hints:
        key = (h["blocker"], h["blocked"])
        if key in edges or key in seen or h["blocker"] not in issues or h["blocked"] not in issues:
            continue
        seen.add(key)
        suggestions.append(h)
    return {"issues": issues, "edges": sorted(edges), "text_suggestions": suggestions}


# ----------------------------------------------------------------------- analyse

def tarjan_scc(nodes: list[int], succ: dict[int, list[int]]) -> list[list[int]]:
    index, low, on_stack, stack, out = {}, {}, set(), [], []
    counter = [0]

    def strong(v):
        # Iterative Tarjan to avoid recursion limits.
        work = [(v, iter(succ.get(v, [])))]
        index[v] = low[v] = counter[0]; counter[0] += 1
        stack.append(v); on_stack.add(v)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]; counter[0] += 1
                    stack.append(w); on_stack.add(w)
                    work.append((w, iter(succ.get(w, []))))
                    advanced = True
                    break
                elif w in on_stack:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop(); on_stack.discard(w); comp.append(w)
                    if w == node:
                        break
                out.append(sorted(comp))

    for v in nodes:
        if v not in index:
            strong(v)
    return out


def analyse(snap: dict, viewer: str | None, now: dt.datetime) -> None:
    """Adds status, layer, unblock counts and a pick score to every issue."""
    issues = snap["issues"]
    open_ids = [n for n, i in issues.items() if i["state"] == "open"]
    open_set = set(open_ids)
    succ = {n: [] for n in open_ids}
    for a, b in snap["edges"]:
        if a in open_set and b in open_set:
            succ[a].append(b)

    comps = tarjan_scc(sorted(open_ids), succ)
    comp_of = {}
    cycles = []
    for ci, comp in enumerate(comps):
        for v in comp:
            comp_of[v] = ci
        if len(comp) > 1 or (comp[0] in succ.get(comp[0], [])):
            cycles.append(comp)
    # Longest-path layering on the condensation DAG.
    csucc = {ci: set() for ci in range(len(comps))}
    indeg = {ci: 0 for ci in range(len(comps))}
    for a in open_ids:
        for b in succ[a]:
            ca, cb = comp_of[a], comp_of[b]
            if ca != cb and cb not in csucc[ca]:
                csucc[ca].add(cb)
                indeg[cb] += 1
    layer_c = {ci: 0 for ci in range(len(comps))}
    queue = [ci for ci, d in indeg.items() if d == 0]
    while queue:
        ci = queue.pop()
        for cj in csucc[ci]:
            layer_c[cj] = max(layer_c[cj], layer_c[ci] + 1)
            indeg[cj] -= 1
            if indeg[cj] == 0:
                queue.append(cj)
    in_cycle = {v for comp in cycles for v in comp}

    # Transitive open descendants (what finishing this would eventually free up).
    desc_cache: dict[int, set[int]] = {}

    def descendants(v: int) -> set[int]:
        if v in desc_cache:
            return desc_cache[v]
        seen, stack = set(), list(succ.get(v, []))
        while stack:
            w = stack.pop()
            if w in seen or w == v:
                continue
            seen.add(w)
            stack.extend(succ.get(w, []))
        desc_cache[v] = seen
        return seen

    for i in issues.values():
        i["open_blockers"] = [b for b in i["blocked_by"] if issues[b]["state"] == "open"]

    for n, i in issues.items():
        open_blockers = i["open_blockers"]
        if i["state"] != "open":
            i["status"] = "closed"
            i["layer"] = None
        else:
            i["status"] = "blocked" if open_blockers else "ready"
            i["layer"] = layer_c[comp_of[n]]
        i["in_cycle"] = n in in_cycle
        i["in_progress"] = i["state"] == "open" and any(p["state"] == "open" for p in i["prs"])
        if i["state"] == "open":
            d = descendants(n)
            i["unblocks_total"] = len(d)
            # Issues that would become ready the moment this one closes.
            i["unblocks_now"] = sum(1 for b in succ[n] if issues[b]["open_blockers"] == [n])
        else:
            i["unblocks_total"] = 0
            i["unblocks_now"] = 0
        updated = dt.datetime.fromisoformat(i["updated"].replace("Z", "+00:00"))
        created = dt.datetime.fromisoformat(i["created"].replace("Z", "+00:00"))
        i["idle_days"] = (now - updated).days
        i["age_days"] = (now - created).days

        # Pick score: transparent components, refined by Claude afterwards.
        score = 3 * i["unblocks_now"] + i["unblocks_total"]
        label_names = " ".join(l["name"] for l in i["labels"])
        if PRIORITY_HIGH.search(label_names):
            score += 4
        elif PRIORITY_MED.search(label_names):
            score += 2
        if BUG.search(label_names):
            score += 1
        if viewer and viewer in i["assignees"]:
            score += 4
        elif not i["assignees"]:
            score += 1
        else:
            score -= 4
        if i["in_progress"]:
            score -= 3
        i["score"] = score

    snap["cycles"] = cycles


# -------------------------------------------------------------------------- diff

def diff(prev: dict | None, cur: dict) -> dict:
    if not prev:
        return {"first_run": True}
    p, c = prev["issues"], cur["issues"]
    out = {"first_run": False, "previous_fetch": prev["meta"]["fetched_at"],
           "created": [], "closed": [], "reopened": [], "missing": [], "edited": [],
           "deps_added": [], "deps_removed": [], "newly_unblocked": [], "newly_blocked": [], "pr_changes": []}
    for n in sorted(set(c) - set(p)):
        out["created"].append({"n": n, "title": c[n]["title"]})
    # Closed issues age out of the fetch window, so only a vanished open issue
    # (deleted or transferred) counts as missing.
    for n in sorted(set(p) - set(c)):
        if p[n]["state"] == "open":
            out["missing"].append({"n": n, "title": p[n]["title"]})
    for n in sorted(set(p) & set(c)):
        a, b = p[n], c[n]
        if a["state"] == "open" and b["state"] == "closed":
            out["closed"].append({"n": n, "title": b["title"], "reason": b["reason"]})
        elif a["state"] == "closed" and b["state"] == "open":
            out["reopened"].append({"n": n, "title": b["title"]})
        fields = []
        if a["title"] != b["title"]:
            fields.append({"field": "title", "from": a["title"], "to": b["title"]})
        for key in ("assignees",):
            if sorted(a[key]) != sorted(b[key]):
                fields.append({"field": key, "added": sorted(set(b[key]) - set(a[key])), "removed": sorted(set(a[key]) - set(b[key]))})
        la, lb = {l["name"] for l in a["labels"]}, {l["name"] for l in b["labels"]}
        if la != lb:
            fields.append({"field": "labels", "added": sorted(lb - la), "removed": sorted(la - lb)})
        if a.get("milestone") != b.get("milestone"):
            fields.append({"field": "milestone", "from": a.get("milestone"), "to": b.get("milestone")})
        if fields:
            out["edited"].append({"n": n, "title": b["title"], "changes": fields})
        if b["state"] == "open" and a.get("status") == "blocked" and b["status"] == "ready":
            out["newly_unblocked"].append({"n": n, "title": b["title"]})
        if b["state"] == "open" and a.get("status") == "ready" and b["status"] == "blocked":
            out["newly_blocked"].append({"n": n, "title": b["title"], "by": b["open_blockers"]})
        pa = {x["n"]: x for x in a["prs"]}
        for pr in b["prs"]:
            old = pa.get(pr["n"])
            if old is None:
                out["pr_changes"].append({"issue": n, "pr": pr["n"], "change": "linked", "state": pr["state"]})
            elif old["state"] != pr["state"]:
                out["pr_changes"].append({"issue": n, "pr": pr["n"], "change": f"{old['state']} → {pr['state']}"})
            elif old.get("decision") != pr.get("decision"):
                out["pr_changes"].append({"issue": n, "pr": pr["n"], "change": f"review {old.get('decision') or 'none'} → {pr.get('decision') or 'none'}"})
    pe = {tuple(e) for e in prev["edges"]}
    ce = {tuple(e) for e in cur["edges"]}
    # Edges to closed issues come and go as those issues enter or leave the
    # fetch window; that is not a real dependency change.
    def reentered(n):
        return n not in p and c[n]["state"] == "closed"
    out["deps_added"] = [{"blocker": a, "blocked": b} for a, b in sorted(ce - pe)
                         if not (reentered(a) or reentered(b))]
    out["deps_removed"] = [{"blocker": a, "blocked": b} for a, b in sorted(pe - ce)
                           if a in c and b in c]
    out["has_changes"] = any(out[k] for k in out if isinstance(out[k], list))
    return out


# ------------------------------------------------------------------------ storage

def repo_dir(data_dir: Path, repo: str) -> Path:
    d = data_dir / "repos" / repo.replace("/", "__").lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_json(path: Path):
    if path.exists():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return None


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


def int_keys(snap: dict | None) -> dict | None:
    if snap:
        snap["issues"] = {int(k): v for k, v in snap["issues"].items()}
    return snap


def activity(issues: dict, now: dt.datetime, days: int) -> dict:
    since = now - dt.timedelta(days=days)
    def after(ts):
        return ts and dt.datetime.fromisoformat(ts.replace("Z", "+00:00")) >= since
    return {"created": sum(1 for i in issues.values() if after(i["created"])),
            "closed": sum(1 for i in issues.values() if after(i["closed"]))}


# ----------------------------------------------------------------------- commands

def cmd_sync(args) -> int:
    data_dir = Path(args.data_dir).expanduser()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    try:
        repo = resolve_repo(args.repo)
        if args.fixture:
            raw = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
            meta, nodes, deps_supported = raw["meta"], raw["nodes"], raw.get("deps_supported", True)
            if raw.get("now"):
                now = dt.datetime.fromisoformat(raw["now"].replace("Z", "+00:00"))
        else:
            closed_since = now - dt.timedelta(days=CLOSED_WINDOW_DAYS)
            prev_meta = (load_json(repo_dir(data_dir, repo) / "snapshot.json") or {}).get("meta") or {}
            if prev_meta.get("fetched_at"):
                prev_fetch = dt.datetime.fromisoformat(prev_meta["fetched_at"].replace("Z", "+00:00"))
                closed_since = min(closed_since, prev_fetch)
            meta, nodes, deps_supported = fetch_all(repo, closed_since)
    except SyncError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=1))
        return 1

    rdir = repo_dir(data_dir, meta["repo"])
    snap_path = rdir / "snapshot.json"
    prev = int_keys(load_json(snap_path))

    cur = normalize(meta, nodes)
    cur["meta"] = {**meta, "fetched_at": now.isoformat().replace("+00:00", "Z"), "deps_supported": deps_supported}
    analyse(cur, meta.get("viewer"), now)
    changes = diff(prev, cur)
    cur["changes"] = changes

    if prev:
        write_json(rdir / "snapshot.previous.json", prev)
    write_json(snap_path, cur)

    issues = cur["issues"]
    open_issues = [i for i in issues.values() if i["state"] == "open"]
    counts = {
        "total": len(issues),
        "open": len(open_issues),
        "closed": len(issues) - len(open_issues),
        "ready": sum(1 for i in open_issues if i["status"] == "ready"),
        "blocked": sum(1 for i in open_issues if i["status"] == "blocked"),
        "in_progress": sum(1 for i in open_issues if i["in_progress"]),
        "dependencies": len(cur["edges"]),
        "cycles": len(cur["cycles"]),
    }
    history_line = {"fetched_at": cur["meta"]["fetched_at"], **counts,
                    "created_since_last": len(changes.get("created", [])),
                    "closed_since_last": len(changes.get("closed", [])),
                    "missing_since_last": len(changes.get("missing", []))}
    with (rdir / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(history_line) + "\n")

    def brief(i, extra=()):
        d = {"n": i["n"], "title": i["title"], "assignees": i["assignees"],
             "labels": [l["name"] for l in i["labels"]]}
        for k in extra:
            d[k] = i[k]
        return d

    ready = sorted((i for i in open_issues if i["status"] == "ready" and not i["in_cycle"]),
                   key=lambda i: (-i["score"], -i["unblocks_total"], i["n"]))
    bottlenecks = sorted((i for i in open_issues if i["unblocks_total"] > 0),
                         key=lambda i: (-i["unblocks_total"], i["n"]))[:5]
    stale = sorted((i for i in open_issues if i["idle_days"] >= 60), key=lambda i: -i["idle_days"])[:5]

    digest = {
        "ok": True,
        "repo": meta["repo"], "private": meta["private"], "viewer": meta.get("viewer"),
        "fetched_at": cur["meta"]["fetched_at"], "deps_supported": deps_supported,
        "counts": counts,
        "activity": {"last_7_days": activity(issues, now, 7), "last_30_days": activity(issues, now, 30)},
        "changes": changes,
        "candidates": [brief(i, ("score", "unblocks_now", "unblocks_total", "in_progress", "idle_days", "milestone"))
                       for i in ready[:15]],
        "bottlenecks": [brief(i, ("status", "unblocks_total", "open_blockers")) for i in bottlenecks],
        "stale": [brief(i, ("idle_days", "status")) for i in stale],
        "cycles": cur["cycles"],
        "text_suggestions": cur["text_suggestions"][:30],
        "notes_path": str(rdir / "notes.json"),
        "html_path": str(rdir / "issue-graph.html"),
    }
    if not deps_supported:
        digest["warning"] = "This GitHub host has no issue-dependency API; only text hints are available."
    print(json.dumps(digest, ensure_ascii=False, indent=1))
    return 0


def cmd_render(args) -> int:
    data_dir = Path(args.data_dir).expanduser()
    try:
        repo = resolve_repo(args.repo) if args.repo else None
    except SyncError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    if repo is None:
        print(json.dumps({"ok": False, "error": "Pass --repo owner/repo."}))
        return 1
    rdir = repo_dir(data_dir, repo)
    snap = int_keys(load_json(rdir / "snapshot.json"))
    if not snap:
        print(json.dumps({"ok": False, "error": f"No snapshot for {repo}. Run sync first."}))
        return 1

    warnings = []
    notes = {"summary": "", "picks": [], "suggested_edges": []}
    notes_path = Path(args.notes) if args.notes else rdir / "notes.json"
    if notes_path.exists():
        try:
            loaded = json.loads(notes_path.read_text(encoding="utf-8"))
            notes["summary"] = str(loaded.get("summary", "")).strip()
            for p in loaded.get("picks", []) or []:
                n = int(p.get("number", -1))
                if n in snap["issues"] and snap["issues"][n]["state"] == "open":
                    notes["picks"].append({"n": n, "why": str(p.get("why", "")).strip()})
                else:
                    warnings.append(f"pick #{n} dropped: not an open issue")
            edges = {tuple(e) for e in snap["edges"]}
            for s in loaded.get("suggested_edges", []) or []:
                a, b = int(s.get("blocker", -1)), int(s.get("blocked", -1))
                if a in snap["issues"] and b in snap["issues"] and a != b and (a, b) not in edges:
                    notes["suggested_edges"].append({"blocker": a, "blocked": b, "reason": str(s.get("reason", "")).strip()})
                else:
                    warnings.append(f"suggested edge #{a}→#{b} dropped")
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            warnings.append(f"notes.json ignored: {e}")

    history = []
    hist_path = rdir / "history.jsonl"
    if hist_path.exists():
        for line in hist_path.read_text(encoding="utf-8").splitlines():
            try:
                history.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    payload = {
        "meta": snap["meta"],
        "issues": list(snap["issues"].values()),
        "edges": snap["edges"],
        "cycles": snap["cycles"],
        "changes": snap.get("changes", {}),
        "history": history[-60:],
        "notes": notes,
    }
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    html = TEMPLATE.read_text(encoding="utf-8")
    cy = CYTOSCAPE.read_text(encoding="utf-8").replace("</script", "<\\/script")
    html = html.replace("/*__CYTOSCAPE__*/", cy, 1)
    html = html.replace("/*__DATA__*/null", blob, 1)
    html = html.replace("__TITLE__", meta_title(snap["meta"]["repo"]), 1)
    out = Path(args.out) if args.out else rdir / "issue-graph.html"
    out.write_text(html, encoding="utf-8")
    opened = False
    if args.open:
        try:
            opened = webbrowser.open(out.resolve().as_uri())
        except Exception:
            opened = False
    print(json.dumps({"ok": True, "html_path": str(out), "opened": opened, "warnings": warnings}, indent=1))
    return 0


def meta_title(repo: str) -> str:
    return (repo.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) + " · Issue graph"


def main() -> int:
    default_data = os.environ.get("CLAUDE_PLUGIN_DATA") or str(Path.home() / ".claude" / "plugins" / "data" / "issue-graph")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="fetch issues, diff against last snapshot, print digest")
    s.add_argument("--repo", help="owner/repo or GitHub URL (default: repo of the current folder)")
    s.add_argument("--data-dir", default=default_data)
    s.add_argument("--fixture", help=argparse.SUPPRESS)  # offline testing
    s.set_defaults(func=cmd_sync)
    r = sub.add_parser("render", help="build the HTML view from the latest snapshot")
    r.add_argument("--repo", required=True)
    r.add_argument("--data-dir", default=default_data)
    r.add_argument("--notes", help="path to notes JSON (default: <repo dir>/notes.json)")
    r.add_argument("--out", help="output HTML path")
    r.add_argument("--open", action="store_true", help="open the view in the default browser")
    r.set_defaults(func=cmd_render)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
