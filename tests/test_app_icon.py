
from pathlib import Path

def test_app_icon_assets_exist():
    assert Path("assets/CloudMover.png").exists()
    assert Path("assets/CloudMover.icns").exists()

def test_pyinstaller_uses_cloudmover_icon():
    s=Path("CloudMover.spec").read_text()
    assert 'assets" / "CloudMover.icns"' in s
    assert "icon=None" not in s


from test_first_run_safety import qt_classes
from src.cloudmover import gui, state


def test_header_icon_and_existing_orange(qt_classes,tmp_path):
    app,scope=qt_classes
    source=tmp_path/'source';source.mkdir();(source/'one.txt').write_text('test')
    db=tmp_path/'state.db';state.initialize_migration(source,db,'/my-files/Backup')
    w=scope['Window'](db);w.timer.stop()
    try:
        assert not w.windowIcon().isNull()
        assert not w.brand_icon.pixmap().isNull()
        assert w.brand_icon.height()==32
        assert 'QProgressBar::chunk { border-radius:9px; background:#c2410c; }' in w.styleSheet()
        assert 'QProgressBar::chunk { background:#c2410c; }' in w.setup_page.styleSheet()
        assert 'background:#c2410c' in w.styleSheet()  # Existing primary action orange.
    finally:w.close()


def test_frozen_icon_uses_bundled_asset(qt_classes,tmp_path,monkeypatch):
    import shutil
    (tmp_path/'assets').mkdir()
    shutil.copyfile('assets/CloudMover.png',tmp_path/'assets/CloudMover.png')
    monkeypatch.setattr(gui.sys,'frozen',True,raising=False)
    monkeypatch.setattr(gui.sys,'_MEIPASS',str(tmp_path),raising=False)
    assert not gui.cloudmover_icon().isNull()
    (tmp_path/'assets/CloudMover.png').unlink()
    assert gui.cloudmover_icon().pixmap(32,32).isNull()  # No source-tree fallback.
    assert '"CloudMover.png"), "assets")' in Path('CloudMover.spec').read_text()


def test_pingstate_header_persists_across_pages(qt_classes,tmp_path,monkeypatch):
    import shutil
    app,scope=qt_classes
    source=tmp_path/'source';source.mkdir();(source/'one.txt').write_text('test')
    db=tmp_path/'state.db';state.initialize_migration(source,db,'/my-files/Backup')
    w=scope['Window'](db);w.timer.stop();w.show();app.processEvents()
    try:
        logo=w.creator_logo.pixmap()
        original=gui.pingstate_logo()
        assert not logo.isNull()
        assert abs(logo.width()/logo.height()-original.width()/original.height())<0.1
        assert logo.deviceIndependentSize().height()<=24
        assert w.creator_logo.isVisible()
        assert w.creator_branding.y()==34
        assert w.creator_branding.x()>w.width()/2
        w.root_stack.setCurrentWidget(w.setup_page);app.processEvents()
        assert w.creator_logo.isVisible()
        assert not w.brand_icon.pixmap().isNull()
        (tmp_path/'assets').mkdir();shutil.copyfile('assets/Pingstate.png',tmp_path/'assets/Pingstate.png')
        monkeypatch.setattr(gui.sys,'frozen',True,raising=False)
        monkeypatch.setattr(gui.sys,'_MEIPASS',str(tmp_path),raising=False)
        assert not gui.pingstate_logo().isNull()
        assert '"Pingstate.png"), "assets")' in Path('CloudMover.spec').read_text()
    finally:w.close()
