---
name: release
description: "Cut a release of open-harness-router in major.minor.micro form: collect the merged PRs not yet listed in CHANGELOG.md, render the new section, bump the version and open the release PR; then, after that PR is merged, tag its merge commit and publish the GitHub release."
argument-hint: "[major|minor|micro|X.Y.Z] | tag"
disable-model-invocation: true
---

# Release open-harness-router

Run every command from the repository root; `<scratch>` is a scratch directory.

## Read this first

- Two invocations. `/release [major|minor|micro|X.Y.Z]` writes the changelog
  section, bumps the version and opens the release PR; `/release tag` runs after
  the user merges it, tagging the merge commit and publishing the release.
- `git commit`, `git push`, `git tag` and `gh release create` run only in the
  steps below, and in phase 1 only after the `AskUserQuestion` of step 3 is
  answered; phase 2 pushes a tag and nothing else. Never force-push.
- The skill never restarts the router or edits its configuration; the service
  keeps serving the previously started code until someone restarts it.
- "Unreleased" is a set difference, not a date: a merged PR is released only
  once its `#N` is referenced in a section whose version is TAGGED. The top
  section is a draft while its version has no tag -- `collect` returns its PRs
  again and reports that version as `draft_version` (null when the top section
  is tagged). The PR list comes from `gh`, never from `git log`: a rebase merge
  leaves no `#N` in any commit subject.
- Versions are exactly three integers, `X.Y.Z`; tags are `vX.Y.Z`.
- `cli.release` exit codes: 0 ok; 2 usage, a malformed version or a target
  lower than the current one; 3 nothing to do; 4 `gh` or `git` failed.

## Phase 1: cut the release

### 1. Preconditions -- stop on the first failure

```sh
git rev-parse --abbrev-ref HEAD          # must be main
git status --porcelain                   # must be empty
git fetch origin
git rev-list --count HEAD..origin/main   # must be 0
gh auth status
gh pr list --state open --json number,title
```

An open PR titled `chore(release): v<V>` means a release is already in flight:
with no argument, or an argument naming `<V>`, take the re-run path at the end
of step 4; an argument naming another version stops the run.

### 2. Collect and pick the version

```sh
PYTHONPATH=src .venv/bin/python -m cli.release collect > <scratch>/prs.json
```

Exit 3 means nothing is unreleased -- stop. Otherwise show a table of number,
category and entry, plus `previous_tag`, `current_version`, `draft_version` and
`suggested_bump`.

Target version: the argument when it is `X.Y.Z`, and it must not be lower than
`current_version`; otherwise
`PYTHONPATH=src .venv/bin/python -m cli.release version --bump <major|minor|micro>`
with the argument, or with `suggested_bump` when there is none. First release
is the exception: when `previous_tag` is null AND no section exists for
`current_version`, the target is `current_version` itself and nothing is
bumped. On the re-run path the target is the open PR's version.

### 3. One `AskUserQuestion`, two questions

- "Version for this release": the target from step 2 (marked as the proposal),
  the two other bumps with their numbers, and a free-form option.
- "Entry list": accept it, or name the PRs to drop and the entries to reword.

Apply the answer to `<scratch>/prs.json` before rendering: `changelog` copies
each `category` and `entry` verbatim. A PR dropped from the JSON gets no `#N`,
so `collect` offers it again -- drop only what belongs in a later release.

### 4. Branch, render, bump, verify, PR

```sh
git checkout -b chore/release-vX.Y.Z
PYTHONPATH=src .venv/bin/python -m cli.release changelog --version X.Y.Z --input <scratch>/prs.json --write
PYTHONPATH=src .venv/bin/python -m cli.release bump --to X.Y.Z
uv lock                                  # only when bump printed: uv.lock: run 'uv lock'
make full-check
git diff --stat                          # expected: CHANGELOG.md, pyproject.toml, uv.lock
```

A red `make full-check` or an unexpected file in the diff stops the release.
Then commit, push and open the PR with the section as its body:

```sh
git add CHANGELOG.md pyproject.toml uv.lock
git commit -m "chore(release): vX.Y.Z"   # plus the trailers of git log -1
git push -u origin chore/release-vX.Y.Z
PYTHONPATH=src .venv/bin/python -m cli.release notes --version X.Y.Z > <scratch>/body.md
gh pr create --base main --title "chore(release): vX.Y.Z" --body-file <scratch>/body.md
```

Stop here: the user merges the PR.

Re-run path, when an open release PR for this version exists: instead of
creating the branch, `git checkout chore/release-vX.Y.Z && git pull --ff-only`,
then repeat steps 2 to 4 on it. The top section carries no tag, so
`changelog --write` replaces it in place. Commit
`chore(release): vX.Y.Z (refresh)` and push to the same branch -- no
force-push, no second PR.

## Phase 2: tag and publish

### 1. Sync and find the release PR

```sh
git checkout main && git pull --ff-only
gh pr list --state merged --search "chore(release): vX.Y.Z in:title" --json number,mergeCommit,mergedAt
```

X.Y.Z is the top `## [X.Y.Z]` section of `CHANGELOG.md`. All of these must
hold, or stop:

```sh
PYTHONPATH=src .venv/bin/python -m cli.release version   # equals X.Y.Z
git tag --list vX.Y.Z                                    # empty
git ls-remote --tags origin vX.Y.Z                       # empty
git merge-base --is-ancestor <mergeCommit> origin/main   # exit 0
```

### 2. Staleness check

```sh
PYTHONPATH=src .venv/bin/python -m cli.release collect > <scratch>/after.json
```

`vX.Y.Z` is not tagged yet, so `after.json` holds the section's own PRs plus
everything merged since. Stale = a PR in `after.json` that the top section does
not list AND whose `merged_at` is earlier than the release PR's `mergedAt`: its
code is inside the tag, its entry is not in the notes. Stop and name those PRs;
PRs merged later belong to the next release. Remedy: re-run phase 1 for the
same version -- the section is untagged, so it is regenerated -- title that PR
`chore(release): vX.Y.Z notes`, merge it and tag THAT merge commit.

### 3. Tag the merge commit and publish

```sh
git tag -a vX.Y.Z -m "vX.Y.Z" <mergeCommit>
git push origin vX.Y.Z
PYTHONPATH=src .venv/bin/python -m cli.release notes --version X.Y.Z > <scratch>/notes.md
gh release create vX.Y.Z --title vX.Y.Z --target <mergeCommit> --notes-file <scratch>/notes.md
gh release view vX.Y.Z --json tagName,url
```

### 4. Report

Version, tag, tagged commit, release URL, the section text, and the note that
the running service keeps the previously started code until it is restarted.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `gh auth status` fails, or a `gh` call exits 4 | no GitHub credentials in this environment | `gh auth login`, then restart the phase; never fall back to `git log` for the PR list |
| `collect` exits 3 in phase 1 | every merged PR is already referenced in `CHANGELOG.md` | nothing to release; stop |
| `changelog --write` exits 3, or `git tag --list vX.Y.Z` is non-empty | the version is already tagged, or its section is not the top one | released versions are frozen: pick the next version instead of editing the section |
| the release PR is not in `gh pr list --state merged` | it is still open or was closed | wait for the merge; phase 2 does nothing until then |
| `bump` exits 2 | the requested version is lower than the current one | choose a version above `current_version`; the version never moves backwards |
| phase 2 stops on the staleness check | a PR was merged while the release PR was open, so its code is inside the tag but not in the section | re-run phase 1 for the same version, merge `chore(release): vX.Y.Z notes`, tag that merge commit |
| a PR reappears in `collect` although it was released | a TAGGED section was edited and its `#N` link vanished (for the untagged top section this is normal: its PRs return until the tag exists) | restore the link in that section; reword the text, keep `([#N](url))` |
| step 1 reports a branch other than `main` | the release was started from a feature branch | `git checkout main && git pull --ff-only`, then start again |

## Forbidden

- Tagging before the release PR is merged, or any commit but its merge commit.
- Editing a section whose tag already exists.
- Releasing with a dirty working tree or a red `make full-check`.
- Force-pushing anything; running the release from a branch other than `main`.
- `git commit`, `git push` or `git tag` outside the steps above.
- Restarting the router or editing its configuration.

## Examples

`/release minor` -- phase 1:

```sh
PYTHONPATH=src .venv/bin/python -m cli.release collect > <scratch>/prs.json
PYTHONPATH=src .venv/bin/python -m cli.release version --bump minor      # 0.2.0
git checkout -b chore/release-v0.2.0
PYTHONPATH=src .venv/bin/python -m cli.release changelog --version 0.2.0 --input <scratch>/prs.json --write
PYTHONPATH=src .venv/bin/python -m cli.release bump --to 0.2.0 && uv lock && make full-check
gh pr create --base main --title "chore(release): v0.2.0" --body-file <scratch>/body.md
```

`/release tag` -- phase 2, after the merge:

```sh
git checkout main && git pull --ff-only
gh pr list --state merged --search "chore(release): v0.2.0 in:title" --json number,mergeCommit,mergedAt
git tag -a v0.2.0 -m "v0.2.0" <mergeCommit> && git push origin v0.2.0
gh release create v0.2.0 --title v0.2.0 --target <mergeCommit> --notes-file <scratch>/notes.md
gh release view v0.2.0 --json tagName,url            # tagName v0.2.0, url ...
```
