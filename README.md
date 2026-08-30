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

## Requirements

- Windows 10/11
- Python 3.11+ (only if running/building from source - the packaged build has no prerequisites)

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
```

Produces a self-contained folder at `dist\FireshareAgent\` (onedir build - no Python install
required on the target machine). Run `dist\FireshareAgent\FireshareAgent.exe`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

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

## License

[GPLv3](LICENSE). Not affiliated with the [Fireshare](https://github.com/ShaneIsrael/fireshare)
project.
