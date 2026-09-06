# fireshare-agent

A Windows tray app that watches your NVIDIA ShadowPlay clips & screenshots folders and
automatically uploads new captures to your [Fireshare](https://github.com/ShaneIsrael/fireshare)
instance.

## Features

- Runs quietly in the system tray - no window on launch. Left-click the tray icon to open the
  main window, which shows what's uploading right now (with a live progress bar, transfer
  rate and ETA), what's already been uploaded, anything waiting on a decision from you, and
  buttons for Sync Now, Pause, Settings and Exit.
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
- Copy the Fireshare share link for anything you've uploaded, straight from the main window -
  the point of recording a clip is usually to send it to someone.
- After a successful upload, choose to leave the file in place, move it to a subfolder, or delete
  it. If a file was matched to one already on the server by filename alone, nothing is moved or
  deleted automatically - it's parked in the main window's **Needs review** list for you to decide
  per file.
- The activity list refreshes itself while the window is open, can be filtered (All / Uploaded /
  Needs review / Failed) and searched by name or folder, and says "No files left to upload" once
  the queue is clear.
- Your Fireshare password is stored in Windows Credential Manager, never in the config file.
- Checks GitHub Releases for a newer version on startup (configurable) and lets you update from
  the tray menu with one click - see [Updating](#updating) below.

## Installation

Requires Windows 10 or 11.

1. Grab the latest `FireshareAgent-Setup-<version>.exe` from the
   [Releases page](https://github.com/J-Stuff/fireshare-agent/releases/latest).
2. Run it. You'll be asked to choose:
   - **Install for me only** (default) - no admin rights needed, installs under your user profile.
   - **Install for all users** - requires admin rights, installs under `Program Files`.
3. The app launches automatically once installed, minimized to the system tray - it has no
   visible window on startup.

The installer isn't code-signed, so Windows SmartScreen may show an "unrecognized app" warning the
first time you run it. Click **More info**, then **Run anyway** to proceed.

## First-time setup

The app does nothing until you tell it what to watch and where to upload. Right-click the tray
icon and choose **Open Settings**:

- **General** tab: add one or more folders to watch (e.g. your ShadowPlay clips/screenshots
  folder) and choose what happens to a file after it's uploaded (leave it, move it to a subfolder,
  or delete it).
- **Fireshare Account** tab: enter your Fireshare server URL, username, and password, then click
  **Test Connection** to confirm it works before saving.
- **Advanced** tab: startup, notification, and update-check preferences.

Click **Save** and the agent starts watching immediately - no restart needed.

## The main window

Left-click the tray icon (or right-click > **Open Fireshare Agent**) to open it. It has four
parts:

- **Status** - what the agent is doing right now: the file being uploaded with a progress bar,
  how much has transferred, the current rate and an estimated time remaining, plus how many files
  are queued behind it. When there's nothing to do it says so: *No files left to upload*.
- **Needs review** - files matched to something already on Fireshare by filename only. That match
  can't be verified exactly (Fireshare exposes neither a size nor a content hash for existing
  files), so the agent won't move or delete your local copy on its own. Choose **Keep**, **Move**
  or **Delete** per file. This section is hidden when the list is empty.
- **Activity** - everything the agent has recorded, newest first, with local timestamps. It
  refreshes on its own while the window is open, and you can filter it (All / Uploaded / Needs
  review / Failed) or search by filename or folder. Click a row to select it, then **Copy Link**
  to put its Fireshare share link on your clipboard (or double-click the row, or right-click it
  for the same options plus **Copy File Path**).
- **Buttons** - **Sync Now** (rescan every watch folder immediately), **Pause** / **Resume**,
  **Settings**, and **Exit Agent** (stops the agent completely).

Closing the window with the X just hides it - the agent keeps watching in the background. Only
**Exit Agent**, or **Exit** in the tray menu, actually stops it.

Hovering the tray icon shows the same status in short form, so you can check on a long upload
without opening anything: *Fireshare Agent - uploading clip.mp4 (43%)*.

### Copying a share link

Select an uploaded row in **Activity** and click **Copy Link**. The agent looks the file up on
your Fireshare server and copies its public link (`.../w/<id>` for a video, `.../i/<id>` for an
image), respecting the **Shareable Link Domain** setting if your Fireshare admin has configured
one. The link is remembered afterwards, so copying it again is instant.

Fireshare's upload API doesn't hand back a link when an upload finishes - the server processes the
file in the background and creates its entry a moment later. So a link asked for within a few
seconds of a large upload can briefly be unavailable, and the agent says so
(*"Fireshare hasn't finished processing X yet - try again in a moment"*) rather than reporting a
failure. Wait a moment and click again.

Failed uploads have no link, so **Copy Link** is disabled for them.

## Updating

By default, the app checks GitHub for a newer release on startup and shows a tray notification
plus an "Update to \<version\> Now" item in the tray menu when one's available. Clicking it asks
you to confirm, then downloads and verifies the new installer and runs it silently - it closes the
app, replaces its files, and relaunches it automatically. If you installed for all users, you'll
see a Windows admin (UAC) prompt as part of that, since updating files under `Program Files`
requires it.

You can also check manually via **Open Settings > Advanced > Check for Updates Now**, or just
download and run a newer installer from the Releases page yourself at any time.

## Where your data lives

Regardless of install mode, your settings, upload history, and logs live in your own user
profile - never under `Program Files`, even for an all-users install:

- Settings: `%AppData%\FireshareAgent\config.json` (your Fireshare password is not in this file)
- Upload history (dedupe manifest): `%AppData%\FireshareAgent\manifest.db`
- Logs: `%AppData%\FireshareAgent\agent.log`
- Password / saved session: Windows Credential Manager

Uninstalling (via **Settings > Apps**, or the shortcut in the Start Menu folder) removes the
installed program files but leaves the above in place, so reinstalling later picks up where you
left off. Delete the `%AppData%\FireshareAgent` folder yourself if you want a clean slate.

## Notes

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

## Development

Looking to run this from source, build the installer yourself, or understand how releases and
auto-updates are wired up? See [docs/](docs/):

- [docs/development.md](docs/development.md) - running from source, building the installer,
  running tests.
- [docs/releasing.md](docs/releasing.md) - how automatic versioning and GitHub Releases work.
- [docs/technical-notes.md](docs/technical-notes.md) - implementation notes on the upload protocol
  and the self-updater.

## License

[GPLv3](LICENSE). Not affiliated with the [Fireshare](https://github.com/ShaneIsrael/fireshare)
project.
