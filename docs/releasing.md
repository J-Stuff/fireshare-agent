# Releasing & Versioning

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

   **Commit messages on `main` need to use one of these prefixes.** If every commit since the
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