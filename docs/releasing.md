# Releasing & Versioning

## Branching

**One short-lived branch per change, merged with a merge commit, then deleted.** There is no
long-lived `dev` branch.

```bash
git checkout main && git pull
git checkout -b feat/whatever        # or fix/, chore/, docs/
# work; one logical change per commit, each with a conventional prefix
git push -u origin feat/whatever
# open a PR against main, then use "Create a merge commit"
git checkout main && git pull && git branch -d feat/whatever
```

**Do not squash-merge.** A squash builds a brand-new commit on `main` with no ancestry link back
to the branch, so git can never tell the branch's commits are already in `main` - every later PR
re-lists them, and the branch has to be reset and force-pushed to recover. Combined with the fact
that semantic-release commits `CHANGELOG.md`, `pyproject.toml` and `__init__.py` to `main` and
nowhere else, a recycled branch also conflicts on those three files after every single release.
A merge commit keeps ancestry intact, so none of that arises.

Squashing also collapses a batch of individually-scoped commits into one line, which costs the
per-commit detail in the changelog - see below.

Verified against python-semantic-release 10.6.2 (the version the workflow pins): a merge commit
whose own message is the non-conventional `Merge pull request #N from ...` is ignored, but the
individual commits behind it are all parsed normally, and the highest bump among them wins.
Breaking changes, chore-only branches (no release), and several PRs merged between releases all
behave correctly.

Relevant GitHub settings (Settings > General > Pull Requests):

- **Allow merge commits** - on.
- **Allow squash merging** - off, so the incompatible option cannot be picked by accident.
- **Automatically delete head branches** - on.

## What runs on a push to main

Versioning and releases are fully automatic - there's no manual tagging step. On every push to
`main`:

1. `.github/workflows/release.yml`'s `test` job runs the test suite.
2. Its `version` job runs [python-semantic-release](https://python-semantic-release.readthedocs.io/),
   which looks at every commit since the last release, classifies each by its
   [Conventional Commits](https://www.conventionalcommits.org/) prefix, and takes the single
   highest-impact bump among them (ten `fix:` commits still only bump patch once, not ten times):

   | Commit prefix | Bump |
   |---|---|
   | `fix:`, `perf:` | patch (`0.1.0` -> `0.1.1`) |
   | `feat:` | minor (`0.1.0` -> `0.2.0`) |
   | `feat!:` / `fix!:` (or any type with `!`), or a `BREAKING CHANGE:` footer | major (`0.1.0` -> `1.0.0`) |
   | `chore:`, `docs:`, `style:`, `refactor:`, `test:`, `build:`, `ci:` | none - allowed, but never trigger a release by themselves |

   **Every commit needs one of these prefixes.** With merge commits this is per-commit rather
   than per-PR-title: each commit on the branch is read individually, so one unprefixed commit is
   simply skipped rather than breaking the release. A branch whose commits are *all* unprefixed
   produces no release at all - that is the usual cause of "the actions didn't run".

   Because each commit is read individually, the changelog lists them separately under **Features**
   and **Bug Fixes** instead of collapsing to a single squashed line. If every commit since the
   last release is from that bottom row (or doesn't follow the convention at all), this job's
   `released` output is `false` and nothing below it runs - that push is a no-op for releases,
   though `.github/workflows/ci.yml` still runs the test suite on every push and PR regardless.
3. If a release was warranted, it bumps `fireshare_agent/__init__.py`'s `__version__` and
   `pyproject.toml`'s version, generates a changelog, tags it, and publishes a GitHub Release.
4. The `build` job then checks out that new tag, builds the Windows executable on a
   `windows-latest` runner, packages it into `FireshareAgent-Setup-<version>.exe` with Inno Setup,
   and attaches that installer plus a `.sha256` checksum file to the release that was just
   published. This is the same installer the in-app updater downloads and runs silently, so a
   release isn't usable for auto-update until this job finishes.