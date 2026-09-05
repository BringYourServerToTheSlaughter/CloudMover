
from pathlib import Path

def test_upload_uses_interruptible_process_group():
    s=Path("src/cloudmover/providers/proton.py").read_text()
    assert "start_new_session=True" not in s
    assert "_terminate_child(proc,signal.SIGINT)" in s
    assert "proc.send_signal(first_signal)" in s
    assert "proc.terminate()" in s
    assert "_run_interruptible_upload([" in s

def test_normal_proton_run_is_interruptible_and_bounded():
    s=Path("src/cloudmover/providers/proton.py").read_text()
    assert "def _run_cli_process" in s
    assert "communicate(timeout=timeout)" in s

def test_gui_control_state_uses_live_pid():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "running=bool(engine_live)" in s
    assert 'self.resume_btn.setText("Running")' in s
    assert 'self.pause_btn.setText("Pause Migration")' in s
    assert 'transition=self.notice.text()' not in s
