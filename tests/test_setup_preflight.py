"""Setup gating exercised with real Qt widgets and a read-only fake Proton CLI."""
import copy
import time

import pytest

from src.cloudmover import gui, state
from src.cloudmover.destination import preflight_destination
from test_destination_regression import remote
from test_first_run_safety import qt_classes


@pytest.fixture
def setup(qt_classes, remote, tmp_path, monkeypatch):
    app, scope = qt_classes
    root = tmp_path / 'iCloud'
    root.mkdir()
    source = root / 'SyncMeBro-2'
    source.mkdir()
    (source / 'example.txt').write_text('original contents')
    scope['default_icloud_path'] = lambda: root
    monkeypatch.setattr(gui, 'default_icloud_path', lambda: root)
    scope['find_proton_drive_cli'] = lambda: 'proton-drive'
    scope['preflight_destination'] = preflight_destination
    monkeypatch.setattr(gui.os, 'statvfs', lambda p: type('Space', (), {'f_bavail': 100_000_000_000, 'f_frsize': 1})())
    dialogs = []
    monkeypatch.setattr(scope['QMessageBox'], 'critical', lambda *args: dialogs.append(args[1:]))
    w = scope['SetupDialog'](tmp_path / 'inventory.db')
    w.consent.setChecked(True)
    w.go_next()
    monkeypatch.setattr(scope['QFileDialog'], 'getExistingDirectory', lambda *args: str(source))
    w.choose_source()
    w.show()
    app.processEvents()
    yield app, scope, w, remote, dialogs
    if w.scan_running():
        w.request_scan_cancel()
        wait_scan(app, w)
    w.close()


def wait_scan(app, w):
    deadline = time.monotonic() + 5
    while w.scan_running() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert not w.scan_running()


def forbid_scan(scope):
    def forbidden(*args, **kwargs):
        pytest.fail('Destination failure must not construct a scan worker or inventory')
    scope['MigrationScanWorker'] = forbidden
    scope['initialize_migration'] = forbidden


@pytest.mark.parametrize('existing_inventory', [False, True])
def test_collision_preserves_setup_inventory_and_remote_contents(setup, existing_inventory):
    app, scope, w, remote, dialogs = setup
    if existing_inventory:
        state.initialize_migration(w.source, w.db_path, '/my-files/Previous')
    before_db = w.db_path.read_bytes() if w.db_path.exists() else None
    source = w.source
    destination = w.destination
    remote.folders.add(destination)
    remote.folder_ids[destination] = 'user-folder'
    remote.files[destination + '/precious.txt'] = {'uid': 'user-file', 'content': 'untouched'}
    before_remote = copy.deepcopy((remote.folders, remote.folder_ids, remote.files))
    forbid_scan(scope)
    w.go_next()
    app.processEvents()
    assert w.pages.currentIndex() == 1
    assert not w.scan_running() and w.scan_worker is None and w.scan_summary is None
    assert w.scan_text.text() == 'Ready to scan.'
    assert not dialogs
    assert 'That Proton folder already exists' in w.dest_help.text()
    assert destination in w.dest_help.text()
    assert "won't merge into or overwrite" in w.dest_help.text()
    assert w.dest_edit.selectedText() == destination
    assert w.source == source and w.consent.isChecked()
    assert w.dest_edit.text() == destination
    assert (w.db_path.read_bytes() if w.db_path.exists() else None) == before_db
    assert (remote.folders, remote.folder_ids, remote.files) == before_remote
    assert all(cmd[2] == 'info' for cmd in remote.calls)


def test_changed_destination_is_rechecked_then_real_inventory_starts(setup):
    app, scope, w, remote, dialogs = setup
    remote.folders.add(w.destination)
    w.go_next()
    w.dest_edit.setText('/my-files/New Backup')
    assert not w.destination_error
    w.go_next()
    assert w.pages.currentIndex() == 2 and not w.scan_running()
    # Inventory and worker construction must follow the second successful check.
    worker_class = scope['MigrationScanWorker']
    def worker(*args):
        assert remote.calls[-1][3] == '/my-files'
        assert sum(cmd[3] == w.destination for cmd in remote.calls) == 2
        return worker_class(*args)
    scope['MigrationScanWorker'] = worker
    w.go_next()
    assert w.scan_running()
    wait_scan(app, w)
    assert w.scan_summary['destination'] == '/my-files/New Backup'
    assert w.scan_summary['source'] == str(w.source)
    assert w.scan_summary['files'] == 1
    assert 'Scan complete' in w.scan_text.text() and not dialogs
    assert all(cmd[2] == 'info' for cmd in remote.calls)


def test_second_destination_collision_is_still_refused(setup):
    _, scope, w, remote, dialogs = setup
    forbid_scan(scope)
    remote.folders.update({w.destination, '/my-files/Also Exists'})
    w.go_next()
    w.dest_edit.setText('/my-files/Also Exists')
    w.go_next()
    assert w.pages.currentIndex() == 1
    assert '/my-files/Also Exists' in w.dest_help.text()
    assert not w.db_path.exists() and not dialogs
    assert w.destination_custom and w.consent.isChecked()


def test_destination_created_while_ready_returns_to_editor(setup):
    _, scope, w, remote, dialogs = setup
    w.go_next()
    assert w.pages.currentIndex() == 2
    remote.folders.add(w.destination)
    forbid_scan(scope)
    w.go_next()
    assert w.pages.currentIndex() == 1
    assert 'already exists' in w.dest_help.text()
    assert 'Scan failed' not in w.scan_text.text()
    assert not w.db_path.exists() and not dialogs


@pytest.mark.parametrize('failure', ['missing', 'unreadable'])
def test_source_preflight_failure_is_source_specific(setup, monkeypatch, failure):
    _, scope, w, remote, dialogs = setup
    forbid_scan(scope)
    if failure == 'missing':
        w.source = w.source / 'missing'
    else:
        monkeypatch.setattr(gui.os, 'access', lambda *args: False)
    w.go_next()
    assert w.pages.currentIndex() == 1
    assert ('unavailable' if failure == 'missing' else 'cannot read') in w.check_text.text()
    assert not w.destination_error and not remote.calls and not dialogs
    assert 'Scan failed' not in w.scan_text.text()
    assert not w.db_path.exists()


def test_scan_operation_failure_is_scan_specific(setup, monkeypatch):
    app, _, w, _, dialogs = setup
    def failed_walk(*args, **kwargs):
        raise OSError('Inventory enumeration failed')
    monkeypatch.setattr(state, 'walk_entries', failed_walk)
    w.go_next()
    w.go_next()
    wait_scan(app, w)
    assert 'Scan failed' in w.scan_text.text()
    assert 'inventory could not be completed' in w.scan_text.text()
    assert dialogs == [('Scan failed', 'Inventory enumeration failed')]
    assert w.pages.currentIndex() == 2 and w.scan_summary is None
    assert not w.db_path.exists()


def test_destination_unavailable_is_inline_setup_failure(setup, monkeypatch):
    _, scope, w, remote, dialogs = setup
    forbid_scan(scope)
    monkeypatch.setattr(remote, 'process', lambda *args, **kwargs: ('', 'Authentication required', 1))
    monkeypatch.setattr('src.cloudmover.providers.proton._run_cli_process', remote.process)
    w.go_next()
    assert w.pages.currentIndex() == 1
    assert 'Could not validate the Proton destination' in w.dest_help.text()
    assert 'Authentication required' in w.dest_help.text()
    assert 'Scan failed' not in w.scan_text.text()
    assert not w.db_path.exists() and not dialogs
