# Development

## Requirements

- Windows 10/11
- Python 3.11+
- [Inno Setup](https://jrsoftware.org/isinfo.php) 6.1+ (only if building the installer)

## Running from source

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe main.py
```

Settings are stored at `%AppData%\FireshareAgent\config.json` (secrets excluded), the dedupe
manifest at `%AppData%\FireshareAgent\manifest.db`, and logs at
`%AppData%\FireshareAgent\agent.log` - the same locations the installed app uses.

## Building a distributable

```powershell
.\.venv\Scripts\python.exe -m PyInstaller packaging\fireshare_agent.spec --noconfirm
iscc /DMyAppVersion=1.2.3 packaging\installer.iss
```

The PyInstaller step produces a self-contained folder at `dist\FireshareAgent\` (onedir build - no
Python install required on the target machine); the `iscc` step packages that folder into a single
installer at `dist\installer\FireshareAgent-Setup-1.2.3.exe` (see
[`packaging/installer.iss`](../packaging/installer.iss)). `MyAppVersion` can be omitted for a local
test compile - it defaults to `0.0.0-dev`.

The installer (Inno Setup) supports both install modes from the same exe:

- **Per-user** (default, no admin required) - installs under `%LocalAppData%\Programs`.
- **Per-machine** (admin required) - installs under `Program Files`.

Running it interactively shows a page letting the user pick; running it with `/CURRENTUSER` or
`/ALLUSERS` on the command line (as the app's self-updater does, matching whichever mode is
already installed) skips that prompt. See [technical-notes.md](technical-notes.md#self-updater)
for how the self-updater drives this.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

## Related docs

- [releasing.md](releasing.md) - how automatic versioning and GitHub Releases are wired up.
- [technical-notes.md](technical-notes.md) - implementation details on the upload protocol and
  the self-updater.
