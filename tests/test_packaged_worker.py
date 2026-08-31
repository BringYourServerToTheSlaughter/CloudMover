
from pathlib import Path

def test_packaged_entrypoint_has_worker_mode():
    s=Path("cloudmover_app.py").read_text()
    assert '"--engine-worker" in sys.argv' in s
    assert "from src.cloudmover.start import main as engine_main" in s
    assert "engine_main()" in s

def test_gui_uses_bundle_worker_when_frozen():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'getattr(sys,"frozen",False)' in s
    assert 'cmd=[sys.executable,"--engine-worker",*common]' in s

def test_gui_keeps_python_module_worker_in_dev():
    s=Path("src/cloudmover/gui.py").read_text()
    assert 'cmd=[sys.executable,"-m","src.cloudmover.start",*common]' in s

def test_bundle_worker_does_not_use_python_dash_m():
    s=Path("src/cloudmover/gui.py").read_text()
    frozen=s.index('if getattr(sys,"frozen",False):')
    dev=s.index("else:", frozen)
    block=s[frozen:dev]
    assert '"-m"' not in block

def test_start_is_hidden_import_for_bundle():
    s=Path("CloudMover.spec").read_text()
    assert '"src.cloudmover.start"' in s
