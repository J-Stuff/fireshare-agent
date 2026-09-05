# Technical Notes

Implementation details and rationale that don't change how the app behaves for a user, but matter
if you're working on the code.

## Fireshare upload protocol

- Fireshare's chunked upload endpoint doesn't validate the checksum field against file content -
  it's only used server-side to group chunk parts for one upload, so the agent sends a random
  per-upload token rather than a real file hash.
- The server-side duplicate check (see the "duplicate protection" feature in the main README)
  matches on filename (normalized, case/punctuation-insensitive) and extension, not a content
  hash - Fireshare's API doesn't expose file size or a hash for existing videos/images. It's
  deliberately conservative: if it can't get a clear answer (network hiccup, etc.), it lets the
  upload proceed rather than risk skipping a genuinely new file.
- Each chunk of a video upload is sent under an identifier derived from the file's path and size,
  not a random one per attempt - so if an upload fails partway and gets retried, it resends into
  the same in-progress group on the server instead of abandoning the chunks already sent (which
  Fireshare only cleans up on a successful reassembly or a server restart, otherwise leaving them
  on disk indefinitely).

## Self-updater

`fireshare_agent/updater.py` checks GitHub Releases for the repo's latest release and, once the
user confirms, hands off to the installer rather than patching files in place itself:

- It downloads the release's installer exe and, if the release published a `.sha256` alongside
  it, verifies the checksum before touching anything - a mismatch raises and the currently
  installed files are left untouched.
- Since a running exe can't overwrite its own files, applying an update launches the downloaded
  installer with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /FORCECLOSEAPPLICATIONS` and then
  quits. The installer (`packaging/installer.iss`) closes the app if it's still running, replaces
  the installed files, and relaunches it (see its `[Run]` section - deliberately not gated behind
  `postinstall`/`skipifsilent`, since a silent run needs to relaunch just as much as an
  interactive one).
- It also passes `/CURRENTUSER` or `/ALLUSERS`, detected by checking whether the running exe's
  install directory sits under a `Program Files` path (`_is_all_users_install()`), so the
  installer repeats whichever mode the app was originally installed with instead of prompting for
  it again on what's meant to be an unattended update. A per-machine install still triggers a UAC
  prompt for the installer itself, since replacing files under `Program Files` requires it, but no
  wizard UI appears.
- `check_for_update()` is a no-op when running from source (`sys.frozen` is unset) - there's no
  installed exe directory to update in place.
