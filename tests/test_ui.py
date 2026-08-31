
from src.cloudmover.ui import progress_bar

def test_progress_bar_truthful_percentage():
    b=progress_bar(25,100,20)
    assert "25.0%" in b
    assert b.count("█")==5

def test_progress_bar_handles_zero_total():
    assert "%" not in progress_bar(0,0,10)


from src.cloudmover.ui import stage_line, batch_summary

def test_stage_line_icons():
    assert "✓" in stage_line("Hydrating","done","45.0s")
    assert "·" in stage_line("Verifying","waiting")

def test_batch_summary():
    text=batch_summary(250*1024**2,45.0,92.0,7.0)
    assert "250.0 MB" in text
    assert "Uploading" in text


def test_activity_uses_real_carriage_return():
    from pathlib import Path
    source = Path("src/cloudmover/ui.py").read_text()
    assert 'sys.stdout.write(f"\\r{self.label}' in source
    assert "\\033[K" in source
    assert "^R" not in source
