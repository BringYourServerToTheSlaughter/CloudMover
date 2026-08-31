from types import SimpleNamespace

from src.cloudmover.providers.icloud import HydrationState, SF_DATALESS, inspect_file


def test_dataless_flag_is_cloud_only():
    st = SimpleNamespace(st_flags=SF_DATALESS, st_size=1234, st_blocks=0)
    state = inspect_file("ignored", stat_result=st)
    assert state.hydration is HydrationState.CLOUD_ONLY
    assert state.logical_bytes == 1234


def test_no_dataless_flag_is_local():
    st = SimpleNamespace(st_flags=0, st_size=1234, st_blocks=2)
    state = inspect_file("ignored", stat_result=st)
    assert state.hydration is HydrationState.LOCAL
    assert state.allocated_bytes == 1024
