from pathlib import Path
import os
import pytest
from src.cloudmover import cli_discovery, gui
from src.cloudmover.providers import proton


@pytest.mark.parametrize('installed',['/usr/local/bin/proton-drive','/opt/homebrew/bin/proton-drive'])
def test_finder_path_gui_and_worker_use_same_executable(monkeypatch,installed):
    restricted='/usr/bin:/bin:/usr/sbin:/sbin'
    monkeypatch.setenv('PATH',restricted)
    monkeypatch.setattr(Path,'is_file',lambda p:str(p)==installed)
    monkeypatch.setattr(cli_discovery.os,'access',lambda path,mode: str(path)==installed and mode==os.X_OK)
    assert gui.find_proton_drive_cli()==installed
    assert proton.cli()==installed
    calls=[]
    monkeypatch.setattr(proton,'_run_cli_process',lambda argv,**kw:(calls.append(argv) or ('ok','',0)))
    assert proton.run(['filesystem','info','/my-files/a folder'])=='ok'
    assert calls==[[installed,'filesystem','info','/my-files/a folder']]
    assert os.environ['PATH']==restricted


def test_terminal_path_has_priority(monkeypatch,tmp_path):
    executable=tmp_path/'proton-drive';executable.write_text('#!/bin/sh\nexit 0\n');executable.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path))
    monkeypatch.setattr(Path,'is_file',lambda p:pytest.fail('fallback must not run when PATH succeeds'))
    assert proton.cli()==str(executable)
    assert gui.find_proton_drive_cli()==str(executable)


@pytest.mark.parametrize('is_file,executable',[(False,True),(True,False),(False,False)])
def test_invalid_standard_locations_fail_closed(monkeypatch,is_file,executable):
    monkeypatch.setenv('PATH','/usr/bin:/bin:/usr/sbin:/sbin')
    monkeypatch.setattr(Path,'is_file',lambda p:is_file)
    monkeypatch.setattr(cli_discovery.os,'access',lambda path,mode:executable)
    assert gui.find_proton_drive_cli() is None
    with pytest.raises(RuntimeError,match='proton-drive CLI not found'):
        proton.cli()
