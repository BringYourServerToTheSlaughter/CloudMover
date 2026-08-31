
from pathlib import Path

def test_upload_uses_interruptible_process_group():
    s=Path("src/cloudmover/providers/proton.py").read_text()
    assert "start_new_session=True" in s
    assert "os.killpg(proc.pid,signal.SIGINT)" in s
    assert "os.killpg(proc.pid,signal.SIGTERM)" in s
    assert "_run_interruptible_upload([" in s

def test_normal_proton_run_remains_mockable():
    s=Path("src/cloudmover/providers/proton.py").read_text()
    assert "p=subprocess.run(cmd,text=True,capture_output=True)" in s

def test_gui_control_state_uses_live_pid():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "running=bool(engine_live)" in s
    assert 'self.resume_btn.setText("Running")' in s
    assert 'self.pause_btn.setText("Pause Migration")' in s
    assert 'transition=self.notice.text()' not in s
