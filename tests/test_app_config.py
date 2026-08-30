from fireshare_agent.config.app_config import AppConfig, WatchFolderConfig


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
