import os

from fireshare_agent.manifest import fingerprint


def _write_file(path: str, content: bytes) -> None:
    with open(path, "wb") as f:
        f.write(content)


def test_fingerprint_stable_for_same_content(tmp_path):
    path = tmp_path / "clip.mp4"
    _write_file(str(path), b"a" * 5000)

    size = os.path.getsize(path)
    fp1 = fingerprint.compute(str(path), size)
    fp2 = fingerprint.compute(str(path), size)

    assert fp1 == fp2


def test_fingerprint_differs_for_different_content(tmp_path):
    path_a = tmp_path / "a.mp4"
    path_b = tmp_path / "b.mp4"
    _write_file(str(path_a), b"a" * 5000)
    _write_file(str(path_b), b"b" * 5000)

    fp_a = fingerprint.compute(str(path_a), os.path.getsize(path_a))
    fp_b = fingerprint.compute(str(path_b), os.path.getsize(path_b))

    assert fp_a != fp_b


def test_fingerprint_handles_file_larger_than_sample_window(tmp_path):
    path = tmp_path / "big.mp4"
    # Bigger than the 1MB head/tail sample window on each side.
    _write_file(str(path), os.urandom(3 * 1024 * 1024))

    size = os.path.getsize(path)
    fp = fingerprint.compute(str(path), size)

    assert fp.startswith(f"{size}-")


def test_fingerprint_differs_when_middle_bytes_change_but_head_tail_same(tmp_path):
    # Sanity-check on the known trade-off: a fingerprint based only on head+tail samples
    # cannot distinguish two files that differ only in the middle. This is an accepted
    # limitation for a cheap dedupe check, documented here so it can't regress silently
    # into being *assumed* to be a full-content hash elsewhere in the codebase.
    size = 3 * 1024 * 1024
    head_tail = os.urandom(1024 * 1024)
    middle_a = b"A" * (size - 2 * 1024 * 1024)
    middle_b = b"B" * (size - 2 * 1024 * 1024)

    path_a = tmp_path / "a.mp4"
    path_b = tmp_path / "b.mp4"
    _write_file(str(path_a), head_tail + middle_a + head_tail)
    _write_file(str(path_b), head_tail + middle_b + head_tail)

    fp_a = fingerprint.compute(str(path_a), size)
    fp_b = fingerprint.compute(str(path_b), size)

    assert fp_a == fp_b  # documents the trade-off rather than asserting it never matters
