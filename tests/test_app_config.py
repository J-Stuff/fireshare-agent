import pytest

from fireshare_agent.config.app_config import (
    CHUNK_SIZE_MB_MAX,
    CHUNK_SIZE_MB_MIN,
    MAX_RETRY_ATTEMPTS_MIN,
    RETRY_BACKOFF_MAX_SECONDS,
    RETRY_BACKOFF_MIN_SECONDS,
    UPLOAD_SPEED_LIMIT_KBPS_MAX,
    UPLOAD_SPEED_LIMIT_KBPS_MIN,
    UPLOAD_SPEED_LIMIT_UNLIMITED,
    AppConfig,
    WatchFolderConfig,
)

_MB = 1024 * 1024


def test_round_trip_preserves_values():
    config = AppConfig()
    config.watch_folders = [WatchFolderConfig(path=r"C:\Videos", recursive=False, watch_images=False)]
    config.video_extensions = [".mp4", ".mkv"]
    config.post_upload_action = "delete"
    config.web_api.base_url = "https://fireshare.example.com"
    config.web_api.chunk_size_bytes = 10 * 1024 * 1024

    restored = AppConfig.from_dict(config.to_dict())

    assert restored.watch_folders[0].path == r"C:\Videos"
    assert restored.watch_folders[0].recursive is False
    assert restored.watch_folders[0].watch_images is False
    assert restored.video_extensions == [".mp4", ".mkv"]
    assert restored.post_upload_action == "delete"
    assert restored.web_api.base_url == "https://fireshare.example.com"
    assert restored.web_api.chunk_size_bytes == 10 * 1024 * 1024


def test_from_dict_tolerates_missing_keys():
    restored = AppConfig.from_dict({})

    assert restored.watch_folders == []
    assert restored.post_upload_action == AppConfig().post_upload_action


def test_from_dict_ignores_leftover_smb_ssh_keys_from_older_configs():
    # A config.json saved before SMB/SSH support was removed may still have these top-level
    # keys on disk; loading it must not crash.
    restored = AppConfig.from_dict({
        "smb": {"videos_destination": r"\\server\share"},
        "ssh": {"host": "nas.local"},
        "web_api": {"base_url": "https://fireshare.example.com"},
    })

    assert restored.web_api.base_url == "https://fireshare.example.com"
    assert not hasattr(restored, "smb")
    assert not hasattr(restored, "ssh")


def test_zero_chunk_size_from_a_hand_edited_config_is_clamped():
    # The bug this guards: chunk_size_bytes of 0 survived into the uploader, whose max(1, ...)
    # floor turned it into 1-BYTE chunks - a multi-GB clip would become billions of POSTs.
    restored = AppConfig.from_dict({"web_api": {"chunk_size_bytes": 0}})

    assert restored.web_api.chunk_size_bytes == CHUNK_SIZE_MB_MIN * _MB


@pytest.mark.parametrize("raw", [-1, 0, 1])
def test_retry_backoff_never_loads_below_the_floor(raw):
    # A negative backoff reached threading.Timer(), which treats it as "fire now" - turning the
    # retry schedule into a hot loop against a server that is already struggling.
    restored = AppConfig.from_dict({"retry_backoff_seconds": raw})

    assert restored.retry_backoff_seconds == RETRY_BACKOFF_MIN_SECONDS


def test_absurdly_large_values_are_capped():
    restored = AppConfig.from_dict({
        "retry_backoff_seconds": 999_999,
        "web_api": {"chunk_size_bytes": 5000 * _MB},
    })

    assert restored.retry_backoff_seconds == RETRY_BACKOFF_MAX_SECONDS
    assert restored.web_api.chunk_size_bytes == CHUNK_SIZE_MB_MAX * _MB


def test_zero_retry_attempts_is_raised_to_one():
    # 0 reads like "don't retry" but actually meant every transient blip failed permanently.
    restored = AppConfig.from_dict({"max_retry_attempts": 0})

    assert restored.max_retry_attempts == MAX_RETRY_ATTEMPTS_MIN


def test_non_numeric_values_fall_back_to_the_default_rather_than_crashing():
    # config.json is documented and hand-editable, so a string or null is realistic.
    restored = AppConfig.from_dict({"max_retry_attempts": "lots", "retry_backoff_seconds": None})

    assert restored.max_retry_attempts == AppConfig().max_retry_attempts
    assert restored.retry_backoff_seconds == AppConfig().retry_backoff_seconds


def test_in_range_values_are_left_alone():
    restored = AppConfig.from_dict({
        "max_retry_attempts": 7,
        "retry_backoff_seconds": 45,
        "web_api": {"chunk_size_bytes": 20 * _MB},
    })

    assert restored.max_retry_attempts == 7
    assert restored.retry_backoff_seconds == 45
    assert restored.web_api.chunk_size_bytes == 20 * _MB


# --- Upload speed limit ------------------------------------------------------------------------
#
# The only setting whose valid values have a hole in them: 0 means "no limit", and there is no
# other way to spell that in a numeric field, so a plain two-sided clamp would turn the default
# into the *slowest* allowed limit.


def test_the_speed_limit_defaults_to_unlimited():
    assert AppConfig().web_api.upload_speed_limit_kbps == UPLOAD_SPEED_LIMIT_UNLIMITED
    assert AppConfig.from_dict({}).web_api.upload_speed_limit_kbps == UPLOAD_SPEED_LIMIT_UNLIMITED


def test_a_config_saved_before_the_speed_limit_existed_still_loads():
    restored = AppConfig.from_dict({"web_api": {"base_url": "https://fireshare.example.com"}})

    assert restored.web_api.upload_speed_limit_kbps == UPLOAD_SPEED_LIMIT_UNLIMITED


@pytest.mark.parametrize("raw", [0, -1, -9999])
def test_a_nonpositive_speed_limit_means_unlimited_rather_than_the_floor(raw):
    restored = AppConfig.from_dict({"web_api": {"upload_speed_limit_kbps": raw}})

    assert restored.web_api.upload_speed_limit_kbps == UPLOAD_SPEED_LIMIT_UNLIMITED


def test_a_pointlessly_small_speed_limit_is_raised_to_the_floor():
    # Pacing happens between chunk requests, so a few hundred bytes a second means a 50MB chunk
    # implies a sleep of over a day - an agent that looks permanently wedged.
    restored = AppConfig.from_dict({"web_api": {"upload_speed_limit_kbps": 1}})

    assert restored.web_api.upload_speed_limit_kbps == UPLOAD_SPEED_LIMIT_KBPS_MIN


def test_an_absurd_speed_limit_is_capped():
    restored = AppConfig.from_dict({"web_api": {"upload_speed_limit_kbps": 99_999_999}})

    assert restored.web_api.upload_speed_limit_kbps == UPLOAD_SPEED_LIMIT_KBPS_MAX


def test_a_non_numeric_speed_limit_falls_back_to_unlimited():
    restored = AppConfig.from_dict({"web_api": {"upload_speed_limit_kbps": "fast"}})

    assert restored.web_api.upload_speed_limit_kbps == UPLOAD_SPEED_LIMIT_UNLIMITED


def test_an_in_range_speed_limit_survives_a_round_trip():
    config = AppConfig()
    config.web_api.upload_speed_limit_kbps = 2048

    assert AppConfig.from_dict(config.to_dict()).web_api.upload_speed_limit_kbps == 2048
