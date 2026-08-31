
from src.cloudmover.start import progress_line
def test_eta_uses_session_delta_not_historical_completed():
    s={"complete_files":81,"total_files":1000,"complete_bytes":25_000_000,"total_bytes":1_000_000_000}
    line=progress_line(s,100.0,81,25_000_000)
    assert "calculating..." in line
