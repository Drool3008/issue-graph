# issue-graph

## Install

```
claude plugin marketplace add Drool3008/issue-graph
claude plugin install issue-graph@dhawal-plugins
```

See a demo built from mock data: [docs/demo.html](docs/demo.html)

A Claude Code plugin that reads a GitHub repo's issues and opens an interactive view of which issues block which, what changed since the last sync, and which issue to pick next. It only reads from GitHub.

## What you get

- **Graph view**: every open issue as a node. Dependency arrows are faint white. Click an issue and the arrows to the issues it blocks turn light red, the arrows from its blockers turn solid white, and everything unrelated fades.
- **Schedule view**: columns by dependency depth: Ready now, Next, Wave 3 and so on, plus a column for issues caught in a cycle. Toggle Show closed to add a Done column.
- **Issue panel**: status, assignees, reviewers (from linked pull requests), labels, milestone, parent, age, linked PRs with review state, blocked-by and blocking lists, and how many issues closing it would free.
- **Each sync**: a diff against the previous snapshot (new, closed, reopened, deleted or transferred, edited, dependencies added or removed, newly unblocked, PR changes), a created-vs-closed chart for the last 12 weeks, the open count across syncs, and Claude's summary with up to five recommended picks.
- **Fetch scope**: each sync reads every open issue plus closed issues updated in the last 12 weeks (or since the previous sync), so large repos such as cli/cli sync in about two minutes.
- **Suggested dependencies**: phrases like "blocked by #12" or "depends on #8" in issue bodies are offered to Claude, which keeps only clear ones. They appear as dashed arrows and are never written to GitHub.

## Requirements

- Claude Code
- GitHub CLI, signed in: `gh auth login`
- Python 3.9 or later (standard library only)

For private repos the gh account needs read access. A classic token needs the `repo` scope; a fine-grained token needs read access to Issues, Pull requests and Metadata.

## Run from a local copy

Try it for one session:

```
claude --plugin-dir ./issue-graph
```

Load it in every session by placing the folder at `~/.claude/skills/issue-graph/`.

## Use

```
/issue-graph:map owner/repo
```

Run it with no argument from inside a cloned repo to use that repo. Run it again whenever you want the view refreshed; each run compares against the last snapshot.

## Read-only

The bundled script sends GraphQL queries through `gh` and refuses to send anything else. The skill instructs Claude not to run any command that changes a repo.

## Stored data

Under the plugin data folder (`~/.claude/plugins/data/<plugin id>/repos/<owner__repo>/`):

- `snapshot.json` and `snapshot.previous.json`: the latest and previous state
- `history.jsonl`: one line of counts per sync
- `notes.json`: Claude's summary, picks and suggested dependencies
- `issue-graph.html`: the view, self-contained and usable offline

## Limits

- GitHub allows up to 50 issues per relationship type, and dependencies within one repository only.
- Reviewers come from pull requests linked with a closing keyword (for example "Fixes #12"). PRs that only mention an issue are not counted.
- The view is built for repos up to a few hundred issues.
- Claude Code on the web blocks GitHub's GraphQL API from its sandbox, so run the plugin in a local Claude Code session.
- On GitHub Enterprise Server versions without issue dependencies, only the suggested (text) dependencies are available.

Cytoscape.js is bundled under `assets/vendor/` (MIT licence).
