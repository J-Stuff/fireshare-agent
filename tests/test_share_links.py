"""
Coverage for resolving a file's public Fireshare link (feature-ideas.md #2).

Everything here follows from one fact established by reading the Fireshare server source: the
upload endpoints do not return an identifier. Both `/api/uploadChunked` and `/api/upload/image`
end in a bare `Response(status=201)` with no body, and the row carrying the id is created
*afterwards* by a `fireshare scan-video` process the server spawns separately
(`_launch_scan_video` -> `Popen(..., start_new_session=True)`).

Two consequences the tests below pin down:

  * A link has to be looked back up by filename, so the lookup reuses the same matching that
    `exists_at_destination` does.
  * "No link yet" is a normal, temporary state rather than an error, and must be reported as
    retryable - a user told their upload failed because the scan hadn't finished would go looking
    for a problem that isn't there.
"""
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from fireshare_agent.config.app_config import AppConfig, WatchFolderConfig, WebApiSettings
from fireshare_agent.manifest.store import ManifestStore
from fireshare_agent.models import MediaKind, PendingFile
from fireshare_agent.pipeline.upload_pipeline import UploadPipeline
from fireshare_agent.uploaders.web_api_uploader import WebApiUploader


def _settings(**overrides) -> WebApiSettings:
    settings = WebApiSettings(base_url="https://fireshare.example.com", username="josh")
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _uploader(entries=None, kind_key="videos", config_body=None) -> WebApiUploader:
    """An uploader whose HTTP layer is replaced by canned /api/videos, /api/images and
    /api/config responses."""
    uploader = WebApiUploader(_settings())
    uploader._authenticated = True
    uploader._resume_attempted = True

    def fake_get(url, **kwargs):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        if url.endswith("/api/config"):
            response.json.return_value = config_body if config_body is not None else {}
        else:
            response.json.return_value = {kind_key: entries or []}
        return response

    uploader._session.get = MagicMock(side_effect=fake_get)
    return uploader


def _video(path, video_id, extension="mp4"):
    return {"video_id": video_id, "path": path, "extension": extension}


# ------------------------------------------------------------------ the /api/videos sort bug

def test_the_video_listing_sends_a_sort_parameter():
    """Regression test for a silently broken duplicate check.

    Fireshare's `/api/videos` reads its sort with `request.args.get('sort')` - no default - and
    returns 400 for anything outside its allowlist. This uploader used to send no parameter at
    all, so every video duplicate check got a 400, `raise_for_status` raised, and
    `exists_at_destination` swallowed it into "not a duplicate, upload anyway" - disabling one of
    the agent's two documented layers of duplicate protection for videos. `/api/images` defaults
    the same parameter, which is why images were never affected."""
    uploader = _uploader(entries=[])
    uploader._fetch_existing_entries(MediaKind.VIDEO)

    _, kwargs = uploader._session.get.call_args
    assert kwargs["params"]["sort"] == "updated_at desc"


def test_the_image_listing_sends_the_same_sort():
    uploader = _uploader(entries=[], kind_key="images")
    uploader._fetch_existing_entries(MediaKind.IMAGE)

    _, kwargs = uploader._session.get.call_args
    assert kwargs["params"]["sort"] == "updated_at desc"


# ------------------------------------------------------------------ URL construction

def test_a_video_link_uses_the_watch_path():
    """Fireshare's own UI builds `{base}/w/{video_id}` (app/client/src/common/utils.js)."""
    uploader = _uploader(entries=[_video("HELLDIVERS 2/extraction.mp4", "abc123")])
    file = PendingFile(
        path=r"C:\clips\extraction.mp4", kind=MediaKind.VIDEO, size_bytes=10,
        remote_folder_hint="HELLDIVERS 2",
    )

    assert uploader.resolve_share_url(file) == "https://fireshare.example.com/w/abc123"


def test_an_image_link_uses_the_image_path():
    uploader = _uploader(entries=[{"image_id": "img789", "path": "shot.png", "extension": "png"}], kind_key="images")
    file = PendingFile(path=r"C:\clips\shot.png", kind=MediaKind.IMAGE, size_bytes=10)

    assert uploader.resolve_share_url(file) == "https://fireshare.example.com/i/img789"


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    uploader = _uploader(entries=[_video("clip.mp4", "abc123")])
    uploader._settings.base_url = "https://fireshare.example.com/"
    file = PendingFile(path=r"C:\clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=10)

    assert uploader.resolve_share_url(file) == "https://fireshare.example.com/w/abc123"


def test_a_file_the_server_does_not_know_about_yet_has_no_link():
    """The expected answer immediately after an upload: the 201 came back before the scan that
    creates the row had run."""
    uploader = _uploader(entries=[])
    file = PendingFile(path=r"C:\clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=10)

    assert uploader.resolve_share_url(file) is None


def test_a_row_without_an_id_yields_no_link_rather_than_a_broken_one():
    uploader = _uploader(entries=[{"path": "clip.mp4", "extension": "mp4"}])
    file = PendingFile(path=r"C:\clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=10)

    assert uploader.resolve_share_url(file) is None


def test_the_lookup_bypasses_the_duplicate_check_cache():
    """`_fetch_existing_entries` caches for 60s. A file uploaded seconds ago is by definition
    absent from a cached list, so reusing one would report it as missing forever."""
    uploader = _uploader(entries=[_video("clip.mp4", "abc123")])
    uploader._existing_entries_cache[MediaKind.VIDEO] = (float("inf"), [])  # a poisoned cache
    file = PendingFile(path=r"C:\clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=10)

    assert uploader.resolve_share_url(file) == "https://fireshare.example.com/w/abc123"


def test_a_different_folder_is_a_different_file():
    """Two games' clips can legitimately share a filename; a link to the wrong one is worse than
    no link."""
    uploader = _uploader(entries=[_video("Cyberpunk 2077/clip.mp4", "wrong")])
    file = PendingFile(
        path=r"C:\clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=10,
        remote_folder_hint="HELLDIVERS 2",
    )

    assert uploader.resolve_share_url(file) is None


# ------------------------------------------------------------------ shareable_link_domain

def test_the_admin_configured_share_domain_wins():
    """`ui_config.shareable_link_domain` replaces the server's own address in every link the web
    UI hands out. An agent ignoring it would copy links that work for the user but not for
    whoever they are sending them to."""
    uploader = _uploader(
        entries=[_video("clip.mp4", "abc123")],
        config_body={"shareable_link_domain": "https://share.example.com"},
    )
    file = PendingFile(path=r"C:\clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=10)

    assert uploader.resolve_share_url(file) == "https://share.example.com/w/abc123"


def test_a_share_domain_saved_without_a_scheme_still_produces_an_openable_link():
    """The web UI concatenates the value verbatim, so a scheme-less setting is a real possibility
    - and "example.com/w/abc" is not something a browser or a chat client will open."""
    uploader = _uploader(
        entries=[_video("clip.mp4", "abc123")],
        config_body={"shareable_link_domain": "share.example.com"},
    )
    file = PendingFile(path=r"C:\clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=10)

    assert uploader.resolve_share_url(file) == "https://share.example.com/w/abc123"


def test_an_unreachable_config_endpoint_falls_back_to_the_upload_target():
    """Same thing Fireshare's UI does when the setting is absent. A link built on the server the
    agent uploads to is right for the overwhelming majority of installs."""
    import requests

    uploader = _uploader(entries=[_video("clip.mp4", "abc123")])
    original = uploader._session.get.side_effect

    def get(url, **kwargs):
        if url.endswith("/api/config"):
            raise requests.ConnectionError("nope")
        return original(url, **kwargs)

    uploader._session.get = MagicMock(side_effect=get)
    file = PendingFile(path=r"C:\clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=10)

    assert uploader.resolve_share_url(file) == "https://fireshare.example.com/w/abc123"


def test_the_share_base_is_only_fetched_once():
    """One extra request per link would be paid on every single copy; this is a setting an admin
    changes approximately never."""
    uploader = _uploader(entries=[_video("clip.mp4", "abc123")], config_body={})
    file = PendingFile(path=r"C:\clips\clip.mp4", kind=MediaKind.VIDEO, size_bytes=10)

    uploader.resolve_share_url(file)
    uploader.resolve_share_url(file)

    config_calls = [c for c in uploader._session.get.call_args_list if c[0][0].endswith("/api/config")]
    assert len(config_calls) == 1


# ------------------------------------------------------------------ manifest storage

def test_a_resolved_link_is_stored_and_read_back(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", r"C:\clips\clip.mp4", 10, "web_api")

    store.set_share_url("fp1", "https://fireshare.example.com/w/abc123")

    assert store.get_recent()[0].share_url == "https://fireshare.example.com/w/abc123"


def test_a_row_starts_with_no_link(tmp_path):
    """None means "not looked up yet", which is different from "there is no link" - the id only
    exists once the server has finished processing the upload."""
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", r"C:\clips\clip.mp4", 10, "web_api")

    assert store.get_recent()[0].share_url is None


def test_storing_a_link_bumps_the_revision(tmp_path):
    """Otherwise the window would not redraw to show the newly cached link."""
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", r"C:\clips\clip.mp4", 10, "web_api")

    before = store.revision
    store.set_share_url("fp1", "https://fireshare.example.com/w/abc123")

    assert store.revision != before


def test_a_database_written_before_share_links_existed_upgrades_in_place(tmp_path):
    """CREATE TABLE IF NOT EXISTS will not add a column to a table that already exists, so an
    agent upgraded in place would otherwise fail every query mentioning share_url."""
    db_path = tmp_path / "manifest.db"
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                fingerprint TEXT PRIMARY KEY, path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                updated_at_utc TEXT NOT NULL, method TEXT NOT NULL, status TEXT NOT NULL, error TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO uploads VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("fp1", r"C:\old_clip.mp4", 99, "2026-01-01T00:00:00+00:00", "web_api", "success", None),
        )
    conn.close()

    store = ManifestStore(str(db_path))

    assert store.is_already_handled("fp1") is True   # pre-existing history survives
    assert store.get_recent()[0].share_url is None
    store.set_share_url("fp1", "https://fireshare.example.com/w/abc123")
    assert store.get_recent()[0].share_url == "https://fireshare.example.com/w/abc123"


# ------------------------------------------------------------------ pipeline outcomes

def _pipeline(tmp_path, uploader) -> tuple[UploadPipeline, ManifestStore]:
    watch = tmp_path / "Videos"
    watch.mkdir()
    manifest = ManifestStore(str(tmp_path / "manifest.db"))
    config = AppConfig(watch_folders=[WatchFolderConfig(path=str(watch))], web_api=_settings())
    pipeline = UploadPipeline(manifest, config)
    pipeline._get_or_create_uploader = lambda: uploader
    return pipeline, manifest


class _StubUploader:
    def __init__(self, url=None, error=None):
        self.url = url
        self.error = error
        self.calls = 0

    def resolve_share_url(self, file, force_refresh=True):
        self.calls += 1
        if self.error:
            raise self.error
        return self.url


def test_resolving_a_link_caches_it_on_the_row(tmp_path):
    uploader = _StubUploader(url="https://fireshare.example.com/w/abc123")
    pipeline, manifest = _pipeline(tmp_path, uploader)
    clip = tmp_path / "Videos" / "clip.mp4"
    clip.write_bytes(b"x" * 10)
    manifest.record_success("fp1", str(clip), 10, "web_api")

    outcome = pipeline.resolve_share_url(manifest.get_recent()[0])

    assert outcome.url == "https://fireshare.example.com/w/abc123"
    assert manifest.get_recent()[0].share_url == "https://fireshare.example.com/w/abc123"


def test_an_already_cached_link_makes_no_request(tmp_path):
    uploader = _StubUploader(url="https://fireshare.example.com/w/abc123")
    pipeline, manifest = _pipeline(tmp_path, uploader)
    clip = tmp_path / "Videos" / "clip.mp4"
    clip.write_bytes(b"x" * 10)
    manifest.record_success("fp1", str(clip), 10, "web_api")
    manifest.set_share_url("fp1", "https://fireshare.example.com/w/cached")

    outcome = pipeline.resolve_share_url(manifest.get_recent()[0])

    assert outcome.url == "https://fireshare.example.com/w/cached"
    assert uploader.calls == 0


def test_a_file_the_scan_has_not_reached_is_reported_as_retryable_not_failed(tmp_path):
    """The distinction the whole ShareLinkOutcome type exists for."""
    pipeline, manifest = _pipeline(tmp_path, _StubUploader(url=None))
    clip = tmp_path / "Videos" / "clip.mp4"
    clip.write_bytes(b"x" * 10)
    manifest.record_success("fp1", str(clip), 10, "web_api")

    outcome = pipeline.resolve_share_url(manifest.get_recent()[0])

    assert outcome.url is None
    assert "hasn't finished processing" in outcome.message
    assert "clip.mp4" in outcome.message


def test_a_request_failure_is_reported_rather_than_raised(tmp_path):
    """This runs on a UI-owned thread answering a button press; every failure mode has the same
    correct outcome - say so in the window rather than take the app down."""
    import requests

    pipeline, manifest = _pipeline(tmp_path, _StubUploader(error=requests.ConnectionError("offline")))
    clip = tmp_path / "Videos" / "clip.mp4"
    clip.write_bytes(b"x" * 10)
    manifest.record_success("fp1", str(clip), 10, "web_api")

    outcome = pipeline.resolve_share_url(manifest.get_recent()[0])

    assert outcome.url is None
    assert "Couldn't reach Fireshare" in outcome.message


def test_the_remote_folder_hint_is_reconstructed_for_the_lookup(tmp_path):
    """The link lookup matches on folder as well as name, so it has to rebuild the same hint the
    upload was made with - otherwise a mirrored per-game clip would never be found."""
    captured = {}

    class _Capturing(_StubUploader):
        def resolve_share_url(self, file, force_refresh=True):
            captured["hint"] = file.remote_folder_hint
            return "https://fireshare.example.com/w/abc123"

    pipeline, manifest = _pipeline(tmp_path, _Capturing())
    game_dir = tmp_path / "Videos" / "HELLDIVERS 2"
    game_dir.mkdir()
    clip = game_dir / "clip.mp4"
    clip.write_bytes(b"x" * 10)
    manifest.record_success("fp1", str(clip), 10, "web_api")

    pipeline.resolve_share_url(manifest.get_recent()[0])

    assert captured["hint"] == "HELLDIVERS 2"
