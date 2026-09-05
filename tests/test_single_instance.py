"""
Regression coverage for the missing single-instance guard: nothing stopped "start with Windows"
plus a manual double-launch (or a stuck old process) from running two independent
watchers/upload pipelines against the same Fireshare account and manifest database at once.
"""
import uuid

from fireshare_agent import single_instance


def _unique_name() -> str:
    return f"FireshareAgentTest-{uuid.uuid4()}"


def test_first_acquire_of_a_name_succeeds():
    assert single_instance.acquire(_unique_name()) is True


def test_second_acquire_of_the_same_name_fails():
    name = _unique_name()

    assert single_instance.acquire(name) is True
    assert single_instance.acquire(name) is False  # simulates a second instance launching


def test_different_names_do_not_conflict():
    assert single_instance.acquire(_unique_name()) is True
    assert single_instance.acquire(_unique_name()) is True
