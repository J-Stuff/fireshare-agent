from fireshare_agent.manifest.store import ManifestStore


def test_unknown_fingerprint_is_not_handled(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    assert store.is_already_handled("nope") is False


def test_recorded_success_is_handled(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", r"C:\clip.mp4", 1234, "web_api")

    assert store.is_already_handled("fp1") is True


def test_recorded_failure_is_not_treated_as_handled(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_failure("fp1", r"C:\clip.mp4", 1234, "web_api", "network error")

    assert store.is_already_handled("fp1") is False
    assert store.get_failed_count() == 1


def test_success_after_failure_overwrites_status(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_failure("fp1", r"C:\clip.mp4", 1234, "web_api", "network error")
    store.record_success("fp1", r"C:\clip.mp4", 1234, "web_api")

    assert store.is_already_handled("fp1") is True
    assert store.get_failed_count() == 0


def test_get_recent_orders_newest_first(tmp_path):
    store = ManifestStore(str(tmp_path / "manifest.db"))
    store.record_success("fp1", "a.mp4", 1, "web_api")
    store.record_success("fp2", "b.mp4", 2, "web_api")

    recent = store.get_recent()
    assert [e.fingerprint for e in recent] == ["fp2", "fp1"]
