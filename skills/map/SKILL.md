---
name: map
description: Sync a GitHub repo's issues (public or private, read-only) and open an interactive graph and schedule view of which issues block which, with what changed since the last sync and which issue to pick next. Use when the user asks to map, graph, refresh or review a repo's issues, blockers or dependencies, or asks what issue to work on next.
argument-hint: "[owner/repo or GitHub URL]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/issue_graph.py *)
---

# Issue graph

This skill is strictly read-only towards GitHub. All GitHub access goes through the bundled script, which only sends GraphQL queries. Never run any other `gh` command that could change a repo (no `gh issue edit`, `gh issue comment`, `gh api -X POST/PATCH/DELETE`, no mutations), even if the user asks during this skill; tell them the plugin only reads.

Repo argument: `$ARGUMENTS` (empty means "the repo of the current folder").

## 1. Sync

Run, adding `--repo <argument>` only when an argument was given:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/issue_graph.py sync --data-dir ${CLAUDE_PLUGIN_DATA} --repo <owner/repo>
```

(On Windows use `python` if `python3` is missing.)

Large repos take a minute or more, so run this Bash call with `timeout: 600000`. Progress lines on stderr are normal. It fetches every open issue plus closed issues updated in the last 12 weeks (or since the previous sync).

The script prints one JSON digest. If `"ok": false`, or the command times out or prints no JSON, tell the user what happened in one or two plain sentences and stop. Do not fall back to `gh issue list`, `gh api` or any other way of fetching issues, and do not describe the repo from memory. Common fixes: install the GitHub CLI, run `gh auth login`, check the repo name, or for a private repo make sure the account can read it.

Do not open `snapshot.json` or page through issues yourself; the digest has what you need. Open the snapshot only if the user later asks about one specific issue.

## 2. Write the notes

Write a JSON file to the digest's `notes_path` with this shape:

```json
{
  "summary": "…",
  "picks": [{"number": 12, "why": "…"}],
  "suggested_edges": [{"blocker": 3, "blocked": 7, "reason": "…"}]
}
```

**summary**: two to four plain sentences. State the backlog (open, ready, blocked), what changed since the last sync from `changes` (new, closed, reopened, gone, unblocked, dependency changes; on `first_run` say this is the baseline), and the main bottleneck from `bottlenecks` if any. Mention cycles if `cycles` is non-empty. Use only numbers present in the digest.

**picks**: up to five issues, chosen only from `candidates` (they are all open and unblocked). Start from `score`, then use judgement:
- prefer issues assigned to the viewer, then unassigned ones; skip issues assigned to someone else unless nothing else is ready
- prefer issues with high `unblocks_now` / `unblocks_total`
- prefer priority or bug labels and a near milestone
- deprioritise `in_progress` issues (a PR is already open)
- note when an issue has been idle a long time

Each `why` is one short sentence with the concrete reason, for example "Unblocks #31 and #40 and is assigned to you."

**suggested_edges**: review `text_suggestions`. Keep one only when its `snippet` clearly says one issue must finish before the other. Write `reason` as a short paraphrase of that evidence. Keep at most ten. Do not invent dependencies from titles or guesswork. Use `[]` when none qualify.

## 3. Render and open

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/issue_graph.py render --repo <digest repo> --data-dir ${CLAUDE_PLUGIN_DATA} --open
```

If `warnings` mention dropped picks or edges, fix the notes and render again.

## 4. Reply

Keep the chat reply short (three to six lines): open, ready and blocked counts; the most important changes since the last sync; the top one or two picks with their reason; and the HTML path if `opened` is false so the user can open it. Do not paste the digest.

In the view: Graph and Schedule switch layouts; clicking an issue turns the arrows to issues it blocks light red and arrows from its blockers solid white, and shows status, assignees, reviewers from linked PRs, labels and the blocker lists.
