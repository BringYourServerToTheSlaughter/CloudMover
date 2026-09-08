
from pathlib import Path

def test_single_window_uses_root_stack():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "self.root_stack=QStackedWidget()" in s
    assert "self.root_stack.addWidget(main_page)" in s
    assert "self.root_stack.addWidget(self.setup_page)" in s

def test_new_migration_does_not_exec_modal_setup():
    s=Path("src/cloudmover/gui.py").read_text()
    start=s.index("def new_migration")
    end=s.index("def _setup_finished")
    block=s[start:end]
    setup_block=block[block.index("# Recreate setup state"): ]
    assert ".exec()" not in setup_block
    assert "self.root_stack.setCurrentWidget(self.setup_page)" in setup_block
    assert "self.root_stack.setCurrentWidget(self.setup_page)" in block

def test_main_layout_order_picker_then_status_then_controls():
    s=Path("src/cloudmover/gui.py").read_text()
    picker=s.index("# 1) Picker first")
    status=s.index("# 2) Migration status second")
    controls=s.index("# 3) Controls last")
    assert picker < status < controls

def test_new_migration_ready_wording():
    s=Path("src/cloudmover/gui.py").read_text()
    assert "Choose what to move, then start the migration." in s
