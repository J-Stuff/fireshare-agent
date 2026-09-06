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

## Upload speed limit

`uploaders/throttle.py`'s `RateLimiter` paces transfers to `WebApiSettings.upload_speed_limit_kbps`
(KB/s, 1024-based to match `ui.formatting.format_bytes`; 0 disables it and makes every method a
no-op, so the chunk loop never branches on whether a limit is set).

**It paces between chunk requests, not within one, and that is forced rather than chosen.**
`requests` builds a multipart body by materializing it in full - `RequestEncodingMixin._encode_files`
calls `fp.read()` on anything file-like - and urllib3 then hands the finished body to a single
`sendall`. There is no hook between those two points, so a file wrapper that slept as it was read
would pace nothing: the sleeping would happen while the body was assembled in memory and the socket
write would still go out at line speed. Finer granularity would mean either a new dependency
(`requests_toolbelt.MultipartEncoder`) or hand-building a chunked transfer-encoding body, which
puts Werkzeug's multipart parser and any fronting proxy at risk for a background uploader. So the
limit is an average with a burst of one chunk, and the Settings caption says so and points at the
chunk size field as the way to make it smoother.

Deriving the chunk size from the speed limit - the obvious way to keep bursts short - is
deliberately *not* done. `totalChunks` would then change between retry attempts of the same file,
and Fireshare reassembles a group by concatenating `{checkSum}.part{n}` under a checksum that is
stable per (path, size); mixed granularity within one group reassembles to garbage.

The call order is **wait before, charge after**:

- Charging before a send would stall the first chunk of every upload for a full chunk's worth of
  time before a single byte moved.
- Sleeping after the last chunk would delay the `SUCCEEDED` event, and with it the post-upload
  move/delete, for a file whose bytes the server already has.

Debt is never negative, so idle time is not banked - an agent that sat idle overnight does not open
with a night's worth of unthrottled transfer. And `_settle()` credits elapsed time against the
debt, so a connection already slower than the limit never sleeps at all.

Images are charged but cannot be paced: a screenshot is one unsplittable request. Charging still
holds the limit across a batch, which is the case that matters - a watch folder filling with a few
hundred screenshots at once.

`sleep` is injected into `WebApiUploader` for two reasons: tests drive it against a fake clock, and
`UploadPipeline._interruptible_sleep` backs it with `_stop_event.wait()`. `stop()` gives the upload
worker five seconds, and a pause at a low limit can be minutes - a plain `time.sleep` would turn
every Exit during a throttled upload into a five-second hang.

Because `PipelineStatus`'s rate and ETA are averages over wall-clock elapsed time, they include
the throttle pauses automatically - a limited upload reports the speed it is actually achieving.

## Share links

The upload endpoints return **no identifier**. Both `/api/uploadChunked` and `/api/upload/image`
end in a bare `Response(status=201)` with no body (confirmed against the server source), and the
`Video` row carrying `video_id` is created afterwards by a `fireshare scan-video` process the
server launches separately (`_launch_scan_video` -> `Popen(..., start_new_session=True)`). So the
id does not exist at the moment the upload returns, and there is nothing to read out of the
response even in principle.

`WebApiUploader.resolve_share_url()` therefore looks the file back up by name through the same
`/api/videos` / `/api/images` listing the duplicate check uses, and builds the link the way
Fireshare's own UI does (`app/client/src/common/utils.js`):

- video: `{base}/w/{video_id}`, image: `{base}/i/{image_id}`
- `base` is `ui_config.shareable_link_domain` from `GET /api/config` when an admin has set one,
  otherwise the configured `base_url`. Ignoring that setting would produce links that work for
  the user but not for whoever they send them to. A value saved without a scheme gets the one the
  agent is already using, since the web UI concatenates it verbatim.

Because the row appears asynchronously, "no link yet" is a normal state rather than an error -
hence `ShareLinkOutcome` having three cases (found / not ready / failed) instead of two. Resolved
links are cached in the manifest's `share_url` column, so a second copy costs nothing and the
link survives a restart. The lookup passes `force_refresh=True` to skip the 60s duplicate-check
cache, which by definition predates a file uploaded seconds ago.

Resolution runs on a short-lived thread owned by the *window*, never on the upload worker: it
makes a request that lists every video on the server, and a queue of clips must not stall behind
someone clicking Copy Link.

### The `/api/videos` sort parameter

`/api/videos` reads its sort with `request.args.get('sort')` - **no default** - and returns 400
for anything outside its allowlist. `/api/images` uses `request.args.get('sort', 'updated_at desc')`
and is unaffected. The uploader used to send no parameter at all, so every *video* duplicate check
got a 400 that `raise_for_status()` turned into an exception, which `exists_at_destination()`
swallowed into "not a duplicate, upload anyway" - silently disabling one of the two documented
layers of duplicate protection for the media type that matters most here. Both listings now send
`sort=updated_at desc`, which is on both allowlists. This has been the server's behaviour since at
least Fireshare v1.6.16, so it was never working, rather than having regressed.

### Extensions come back with the dot

Both listings return `extension` as `".mp4"` / `".png"`, dot included. `_find_existing_entry()`
used to build its own side from `Path.suffix` with the dot stripped, so `".mp4" != "mp4"` rejected
every candidate row and the lookup returned `None` for files that were plainly on the server.

That is one comparison breaking two features, because both callers of `_find_existing_entry()`
read a `None`/no-match as an answer rather than as a failure: `resolve_share_url()` reported
"Fireshare hasn't finished processing this yet" *forever* for an already-uploaded clip, and
`exists_at_destination()` reported "not a duplicate, upload anyway" for everything - killing the
server-side duplicate check a second time, by a cause entirely independent of the sort parameter
above. The sort fix got the request through; this rejected the response.

Both sides now go through `_normalize_extension()` (strip whitespace, strip a leading dot,
lowercase), rather than the local side simply learning to keep its dot - neither spelling can
reintroduce the bug that way if the server's shape changes again.

Worth knowing when writing tests here: every fixture in the suite used to spell the extension
without a dot, which is not what a Fireshare returns. Code and tests agreed on the same wrong
assumption, which is exactly why a well-covered path stayed broken. **Fixture rows for
`/api/videos` and `/api/images` must carry the dot.**

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

## Main window refresh model

`fireshare_agent/ui/main_window.py` is the window a left-click on the tray icon opens. It runs
three different refresh cadences on purpose, and the reasons are easy to undo by accident:

- **Status card - polled at 1 Hz.** `UploadPipeline.get_status()` returns an immutable
  `PipelineStatus` built purely from in-memory state, so a poll costs one lock acquisition and no
  I/O. Polling rather than reacting to pushed events matters: a window opened halfway through a
  4 GB upload shows the right percentage immediately instead of waiting for the next chunk to
  land. The snapshot is assembled under a single lock so its numbers agree with each other -
  `queued_count` is derived by subtracting the active and parked files from the in-flight total,
  and is clamped at zero because the in-flight count is read under a different lock a moment
  earlier.
- **History, stats and review queue - every 5 s, but gated on a revision counter.**
  `ManifestStore.revision` is an in-process counter bumped by every write. The window compares it
  and skips the rebuild when nothing has changed, which is almost always. Rebuilding a
  two-hundred-row textbox every five seconds would discard the user's scroll position and text
  selection each time - an auto-refreshing log you can't read is worse than a stale one.
- **Nothing at all while hidden.** Closing the window withdraws rather than destroys it (so
  reopening is instant and keeps the filter, search and scroll position), and `hide()` cancels the
  timer. The tick itself keeps rescheduling while the window is merely *minimized* and only skips
  the drawing, because minimizing never calls `hide()` - returning without rescheduling there
  would leave a restored window frozen with no way to start it ticking again.

Upload progress reaches all of this through an optional `on_progress(bytes_sent, total_bytes)`
callback on `Uploader.upload()`, invoked by the chunk loop after each chunk the server has
accepted. The pipeline records every callback into its snapshot (one assignment) but rate-limits
the broadcast `PROGRESS` activity event to roughly one a second - the activity listener in
`app.py` logs every event, so an event per chunk would flood the size-capped `agent.log`, the
same trap `WAITING` fell into. `PROGRESS` and `WAITING` are both logged at DEBUG, which under the
app's INFO root logger means they are never written to the file at all.

A progress callback that raises is swallowed and logged at DEBUG (`_safe_progress`): the consumer
is UI state, and a multi-gigabyte transfer that is otherwise succeeding has no business failing
because a window was destroyed mid-callback.

## Tray icon click handling

A left-click on the tray icon opens the main window. pystray's Windows backend calls the `Icon`
object on `WM_LBUTTONUP`, and `Icon.__call__` invokes whichever `MenuItem` has `default=True` -
so "Open Fireshare Agent" carries that flag. It has to be an item that is always visible: the
conditional entries above it ("Update to X Now", "Review N File(s)...") are skipped when hidden,
and a default nobody can reach is no default at all.

The tooltip is a live callback rather than a fixed string, so it can carry upload progress. It is
updated through `TrayIcon.refresh_tooltip()` (title only) on progress events, and only through the
full `refresh()` - which rebuilds the icon bitmap and the native menu - when the pause state,
failure badge or review count actually changes.
