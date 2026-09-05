# fireshare-agent

A Windows tray app that watches your NVIDIA ShadowPlay clips & screenshots folders and
automatically uploads new captures to your [Fireshare](https://github.com/ShaneIsrael/fireshare)
instance.

## Features

- Runs quietly in the system tray - no window on launch, configurable from a Settings dialog.
- Watches one or more folders for new clips/screenshots and waits until a file is fully written
  (stable size + an exclusive-open probe) before touching it, so in-progress recordings are never
  copied mid-write. A file that's still growing (e.g. a long manual "Record" session, not just a
  quick Instant Replay clip) is rechecked periodically rather than given up on - a normal
  multi-hour recording will still be picked up once it finishes. Also does a full rescan of all
  watch folders on startup, so nothing created while the app wasn't running gets missed.
- Uploads via the Fireshare Web API - logs in with your Fireshare account and uploads over HTTP.
  Videos go through Fireshare's chunked upload endpoint in configurable sub-100MB pieces, so
  uploads work even through a Cloudflare-fronted instance (Cloudflare caps a single request body
  at 100MB).
- Two layers of duplicate protection: a local SQLite manifest tracks what this agent has already
  uploaded (so restarts and manual rescans never re-upload the same file), and before uploading
  anything new it also checks whether a same-named file already exists on the Fireshare server -
  so a lost/reinstalled local database, or a clip already uploaded some other way, doesn't result
  in a duplicate.
- After a successful upload, choose to leave the file in place, move it to a subfolder, or delete
  it.
- Your Fireshare password is stored in Windows Credential Manager, never in the config file.
- Checks GitHub Releases for a newer version on startup (configurable) and lets you update from
  the tray menu with one click - after you confirm, it downloads the new installer, verifies its
  checksum, and runs it silently (matching whether the app was originally installed per-machine or
  per-user), which closes the app, replaces its files, and relaunches it automatically. Only
  active in the packaged build; a no-op when running from source.

## Requirements

- Windows 10/11
- Python 3.11+ (only if running/building from source - the packaged build has no prerequisites)
- [Inno Setup](https://jrsoftware.org/isinfo.php) 6.1+ (only if building the installer)

## Running from source

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe main.py
```

Settings are stored at `%AppData%\FireshareAgent\config.json` (secrets excluded), the dedupe
manifest at `%AppData%\FireshareAgent\manifest.db`, and logs at
`%AppData%\FireshareAgent\agent.log`.

## Building a distributable

```powershell
.\.venv\Scripts\python.exe -m PyInstaller packaging\fireshare_agent.spec --noconfirm
iscc /DMyAppVersion=1.2.3 packaging\installer.iss
```

The PyInstaller step produces a self-contained folder at `dist\FireshareAgent\` (onedir build - no
Python install required on the target machine); the `iscc` step packages that folder into a single
installer at `dist\installer\FireshareAgent-Setup-1.2.3.exe` (see
[`packaging/installer.iss`](packaging/installer.iss)). `MyAppVersion` can be omitted for a local
test compile - it defaults to `0.0.0-dev`.

The installer ([Inno Setup](https://jrsoftware.org/isinfo.php)) supports both install modes from
the same exe:

- **Per-user** (default, no admin required) - installs under `%LocalAppData%\Programs`.
- **Per-machine** (admin required) - installs under `Program Files`.

Running it interactively shows a page letting the user pick; running it with `/CURRENTUSER` or
`/ALLUSERS` on the command line (as the app's self-updater does, matching whichever mode is
already installed) skips that prompt.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

## Releases & versioning

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

**Breaking changes while the version is still `0.x`**: `pyproject.toml` doesn't override
`major_on_zero`, so it's at PSR's default of `true` - a single breaking-change commit (`feat!:`,
`fix!:`, or a `BREAKING CHANGE:` footer) right now would jump straight from `0.x.y` to `1.0.0`,
same as it would from any other version. If you'd rather stay in `0.x` through breaking changes
during early development and only reach `1.0.0` deliberately, set
`major_on_zero = false` under `[tool.semantic_release]` in `pyproject.toml` - breaking changes
then bump minor instead while the major version is `0`.

## Notes

- Fireshare's chunked upload endpoint doesn't validate the checksum field against file content -
  it's only used server-side to group chunk parts for one upload, so the agent sends a random
  per-upload token rather than a real file hash.
- The server-side duplicate check matches on filename (normalized, case/punctuation-insensitive)
  and extension, not a content hash - Fireshare's API doesn't expose file size or a hash for
  existing videos/images. It's deliberately conservative: if it can't get a clear answer (network
  hiccup, etc.), it lets the upload proceed rather than risk skipping a genuinely new file.
- MFA-enabled Fireshare accounts are supported on a best-effort basis: if the server asks for a
  TOTP code mid-upload, the app pauses that upload and prompts you for the current code. This is
  inherently a manual step for what's otherwise an unattended background service - but it only
  happens once per session. Fireshare's login sets a long-lived "remember me" cookie, which the
  agent persists (in Windows Credential Manager, alongside your passwords) and reuses on every
  restart, so you're not re-entering a TOTP code every time the app starts. It only asks again
  once that saved session actually stops working (expired, revoked, or you change the Fireshare
  URL/username/password in Settings). One thing outside this app's control: if your Fireshare
  server doesn't have `SECRET_KEY` set explicitly in its environment, Fireshare generates a random
  one on every process start - which invalidates every existing session (including this agent's
  saved one) whenever the container restarts. Setting `SECRET_KEY` to a fixed value in your
  Fireshare `docker-compose.yml` avoids that, so a routine container restart doesn't cost you a
  TOTP prompt.
- Each chunk of a video upload is sent under an identifier derived from the file's path and size,
  not a random one per attempt - so if an upload fails partway and gets retried, it resends into
  the same in-progress group on the server instead of abandoning the chunks already sent (which
  Fireshare only cleans up on a successful reassembly or a server restart, otherwise leaving them
  on disk indefinitely).
- The self-updater downloads the new release's installer and, if the release published a
  `.sha256` alongside it, verifies the checksum before touching anything - a mismatch aborts with
  an error and the currently-installed files are left untouched. Since a running exe can't
  overwrite its own files, applying an update launches the downloaded installer with
  `/VERYSILENT` and then quits - the installer closes the app (if it hasn't already exited),
  replaces the installed files, and relaunches it. It's passed `/CURRENTUSER` or `/ALLUSERS`
  depending on whether the app is currently installed per-user or per-machine, so it repeats that
  same choice rather than prompting for it again on what's meant to be an unattended update - a
  per-machine install still triggers a UAC prompt for the installer itself, since replacing files
  under `Program Files` requires it, but no wizard UI appears.

## License

[GPLv3](LICENSE). Not affiliated with the [Fireshare](https://github.com/ShaneIsrael/fireshare)
project.
