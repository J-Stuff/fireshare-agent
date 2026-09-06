# CHANGELOG

<!-- version list -->

## v1.5.1 (2026-09-06)

### Bug Fixes

- Match server file extensions with or without the leading dot
  ([`65224c7`](https://github.com/J-Stuff/fireshare-agent/commit/65224c760ec3b4461b73f3e81396f87c61b994cd))


## v1.5.0 (2026-09-06)

### Chores

- Update .gitignore to add local development files
  ([`70b37a0`](https://github.com/J-Stuff/fireshare-agent/commit/70b37a0a4cc9cbc85d6cb39d1e70f1c0d78230ed))

### Documentation

- Replace the dev-branch/squash workflow with short-lived branches
  ([`395265d`](https://github.com/J-Stuff/fireshare-agent/commit/395265d7e2caad73bba28e8ee1c3e6d2597dfa6d))

### Features

- Added customisable upload speed limit
  ([`287cfaa`](https://github.com/J-Stuff/fireshare-agent/commit/287cfaa534538f332b95b9f2fd83de6e20094fb5))


## v1.4.0 (2026-09-06)

### Bug Fixes

- Background process bugs ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))

- Bug where saving Settings silently un-pauses a paused agent
  ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))

- Chunk size and retry backoff accept 0 and negative values
  ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))

- Crushed files from improper merge conflict resolution
  ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))

- Finish deletion on an inferred server-side match patch
  ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))

- Startup rescan sleeps 3 seconds per already-uploaded file
  ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))

- Sync Now from tray icon blocks the tray thread
  ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))

### Features

- Add CloudFlare detection when setting upload chunk size to notify user if setting is set too high
  ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))

- Add main status window, fix duplicate upload bug, repair failed merge conflict resolution
  ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))

- New status window for uploads and app management. fix: Background search functionality would
  silently swallow 400 errors from the server
  ([#10](https://github.com/J-Stuff/fireshare-agent/pull/10),
  [`d455bfc`](https://github.com/J-Stuff/fireshare-agent/commit/d455bfc589c3d3f2339566807637add0c14ffc39))


## v1.3.0 (2026-09-05)

### Bug Fixes

- Background process bugs
  ([`a37a56a`](https://github.com/J-Stuff/fireshare-agent/commit/a37a56ae733547373d5049a5454a836346f929ae))

- Bug where saving Settings silently un-pauses a paused agent
  ([`a37a56a`](https://github.com/J-Stuff/fireshare-agent/commit/a37a56ae733547373d5049a5454a836346f929ae))

- Chunk size and retry backoff accept 0 and negative values
  ([`a37a56a`](https://github.com/J-Stuff/fireshare-agent/commit/a37a56ae733547373d5049a5454a836346f929ae))

- Finish deletion on an inferred server-side match patch
  ([`a37a56a`](https://github.com/J-Stuff/fireshare-agent/commit/a37a56ae733547373d5049a5454a836346f929ae))

- Startup rescan sleeps 3 seconds per already-uploaded file
  ([`a37a56a`](https://github.com/J-Stuff/fireshare-agent/commit/a37a56ae733547373d5049a5454a836346f929ae))

- Sync Now from tray icon blocks the tray thread
  ([`a37a56a`](https://github.com/J-Stuff/fireshare-agent/commit/a37a56ae733547373d5049a5454a836346f929ae))

### Features

- Add CloudFlare detection when setting upload chunk size to notify user if setting is set too high
  ([`a37a56a`](https://github.com/J-Stuff/fireshare-agent/commit/a37a56ae733547373d5049a5454a836346f929ae))


## v1.2.0 (2026-09-05)

### Bug Fixes

- Add single-instance guard to prevent duplicate agents running
  ([#5](https://github.com/J-Stuff/fireshare-agent/pull/5),
  [`ffa0458`](https://github.com/J-Stuff/fireshare-agent/commit/ffa0458a9411e4e65a697aca282bb3b0fcb1f562))

- Exclude post-upload subfolder from watching and rescans
  ([#5](https://github.com/J-Stuff/fireshare-agent/pull/5),
  [`ffa0458`](https://github.com/J-Stuff/fireshare-agent/commit/ffa0458a9411e4e65a697aca282bb3b0fcb1f562))

- Make failed-upload retry backoff non-blocking
  ([#5](https://github.com/J-Stuff/fireshare-agent/pull/5),
  [`ffa0458`](https://github.com/J-Stuff/fireshare-agent/commit/ffa0458a9411e4e65a697aca282bb3b0fcb1f562))

- Trigger release for installer packaging ([#5](https://github.com/J-Stuff/fireshare-agent/pull/5),
  [`ffa0458`](https://github.com/J-Stuff/fireshare-agent/commit/ffa0458a9411e4e65a697aca282bb3b0fcb1f562))

- Upload bugs regarding moved files, fail-retry backoff & multiple launched instances
  ([#5](https://github.com/J-Stuff/fireshare-agent/pull/5),
  [`ffa0458`](https://github.com/J-Stuff/fireshare-agent/commit/ffa0458a9411e4e65a697aca282bb3b0fcb1f562))

### Features

- Update settings menu visuals ([#5](https://github.com/J-Stuff/fireshare-agent/pull/5),
  [`ffa0458`](https://github.com/J-Stuff/fireshare-agent/commit/ffa0458a9411e4e65a697aca282bb3b0fcb1f562))


## v1.1.0 (2026-09-05)

### Bug Fixes

- Trigger release for installer packaging ([#4](https://github.com/J-Stuff/fireshare-agent/pull/4),
  [`be34060`](https://github.com/J-Stuff/fireshare-agent/commit/be3406039ba79ae38538defeb5b84ee3215dd27b))

### Features

- Update settings menu visuals ([#4](https://github.com/J-Stuff/fireshare-agent/pull/4),
  [`be34060`](https://github.com/J-Stuff/fireshare-agent/commit/be3406039ba79ae38538defeb5b84ee3215dd27b))


## v1.0.1 (2026-09-05)

### Bug Fixes

- Trigger release for installer packaging
  ([`cfc2ac7`](https://github.com/J-Stuff/fireshare-agent/commit/cfc2ac767070f8cd85e9ffb1f3c77ca5f1d84907))


## v1.0.0 (2026-09-05)

- Initial Release
