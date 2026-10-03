# Issue tracker: GitHub Issues

Issues and feature specs for this repo live in GitHub Issues on [`ploono/passnote`](https://github.com/ploono/passnote/issues). Use the `gh` CLI.

The repo is public, so issue text must not contain local paths, session ids, emails or secrets.

## Conventions

- One issue per ticket, titled in plain English, with a body that has `## What`, the design notes or open questions, and `## Acceptance`.
- A larger feature gets a parent issue that holds its spec, or links to a spec file under `docs/superpowers/specs/`. Its implementation tickets are separate issues that reference the parent (`Part of #N`).
- Triage state is a label (see `triage-labels.md`). Use `bug` or `enhancement` for the kind.
- Discussion and decisions go in issue comments. A pull request closes an issue with `Closes #N` in its description.

## Commands

- Create: `gh issue create -R ploono/passnote -t "<title>" -F <body.md> -l <labels>`
- Fetch a ticket: `gh issue view <N> -R ploono/passnote --comments`
- List open work: `gh issue list -R ploono/passnote -l ready-for-agent`
- Comment: `gh issue comment <N> -R ploono/passnote -F <comment.md>`

## History

Before 2026-10-03, tickets were markdown files under `.scratch/<feature>/issues/`. Those files stay in the repo as history; their content now lives in issues #2 (takeover only when gone) and #3 (WAIT / overflow / unanswered). Write new tickets as GitHub issues only.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a parent issue labelled `map`; each **child** ticket is an issue that references it.

- **Map**: an issue whose body holds the Notes, Decisions so far and Fog sections.
- **Child ticket**: an issue with the question in its body. A `Type:` line records the ticket type (`research`, `prototype`, `grilling` or `task`). Being assigned or closed records whether it is claimed or resolved.
- **Blocking**: a `Blocked by: #N, #N` line near the top. A ticket is unblocked when every issue it lists is closed.
- **Frontier**: the open, unassigned, unblocked children of the map, oldest first.
- **Claim**: assign the issue to yourself before any work.
- **Resolve**: post the answer as a comment under an `## Answer` heading, close the issue, then add a pointer (gist plus link) to the map's Decisions so far.
