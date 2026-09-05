
from pathlib import Path

def test_gui_exit_stops_live_engine():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "def stop_engine_for_app_exit" in s
    assert "os.killpg(pid,signal.SIGINT)" in s
    assert "os.killpg(pid,signal.SIGTERM)" in s
    assert "os.killpg(pid,signal.SIGKILL)" in s

def test_window_close_pauses_engine():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "def closeEvent(self,event):" in s
    assert "self.stop_engine_for_app_exit()" in s

def test_terminal_keyboard_interrupt_pauses_engine():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "except KeyboardInterrupt:" in s
    assert "w.stop_engine_for_app_exit()" in s
    assert "SystemExit(130)" in s
