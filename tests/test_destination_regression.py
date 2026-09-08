import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from src.cloudmover import start, state, hydrate, bulk_pipeline
from src.cloudmover.providers import proton
from src.cloudmover.providers.icloud import FileState, HydrationState
from src.cloudmover.event_log import MigrationEventReader

READABLE = 'iCloud Migration - Sep 06, 2026 at 22-36-40'
UID = '86fb19fd-7bca-416b-9e33-659699f6dc4d'
DESTINATION = '/my-files/CloudMover/' + READABLE + ' ' + UID


class FakeProton:
    """Strict argv-driven filesystem: creating a child never creates its parents."""
    def __init__(self):
        self.folders={'/my-files','/my-files/CloudMover'}
        self.files={}
        self.folder_ids={}
        self.calls=[]
        self.fail_create=False
        self.fail_rename=False

    def process(self,cmd,**kwargs):
        assert isinstance(cmd,list) and all(isinstance(a,str) for a in cmd)
        assert cmd[:2]==['proton-drive','filesystem']
        self.calls.append(cmd)
        op=cmd[2];args=cmd[3:]
        if op=='list':
            assert args[0]=='-j'
            if args[1:3]==['-t','file']:
                assert len(args)==4
            else: assert len(args)==2
            path=args[-1]
            if path not in self.folders:
                return '',f'Node not found: {path.rsplit("/",1)[-1]}',1
            return json.dumps([v for p,v in self.files.items() if p.rsplit('/',1)[0]==path]),'',0
        if op=='create-folder':
            assert len(args) in (2,3), 'parentPath and name are two argv elements'
            if len(args)==3: assert args[-1]=='-j'
            parent,name=args[:2]
            if self.fail_create or parent not in self.folders:
                return '',f'Node not found: {parent.rsplit("/",1)[-1]}',1
            path=parent+'/'+name
            if path in self.folders: return '', 'Already exists',1
            self.folders.add(path)
            self.folder_ids[path]='folder-'+str(len(self.folder_ids))
            return json.dumps({'name':name,'uid':self.folder_ids[path]}),'',0
        if op=='upload':
            assert args[:3]==['-f','rename','-t'] and len(args)>=5
            parent=args[-1]
            assert parent in self.folders
            for local_arg in args[3:-1]:
                local=Path(local_arg)
                self.files[parent+'/'+local.name]={
                    'name':local.name,'uid':'node-'+str(123+len(self.files)),'parentUid':'parent-456',
                    'activeRevision':{'claimedSize':local.stat().st_size,'claimedDigests':{'sha1':proton.sha1_file(local)}}}
            return '','',0
        if op=='info':
            assert len(args)==2 and args[1]=='-j'
            remote=self.files.get(args[0])
            if remote is None:
                remote=next((v for v in self.files.values() if '/my-files/'+v['uid']==args[0]),None)
            if args[0] in self.folders:
                path=args[0]
                remote={'name':path.rsplit('/',1)[1],'uid':self.folder_ids.setdefault(path,'existing-'+path),'type':'folder'}
            if remote is None:return '',f'Node not found: {args[0]}',1
            return json.dumps(remote),'',0
        if op=='rename':
            assert len(args)==2
            old,name=args
            if self.fail_rename:return '', 'Rename failed',1
            if old.rsplit('/',1)[0]+'/'+name in self.files:
                return '', 'A node with this name already exists',1
            remote=self.files.pop(old);remote['name']=name
            self.files[old.rsplit('/',1)[0]+'/'+name]=remote
            return '','',0
        if op=='trash':
            assert len(args)==1 and args[0].startswith('/my-files/node-')
            self.files={p:v for p,v in self.files.items() if '/my-files/'+v['uid']!=args[0]}
            return '','',0
        pytest.fail(f'Unexpected command: {cmd}')


@pytest.fixture
def remote(monkeypatch):
    remote=FakeProton()
    monkeypatch.setattr(proton,'cli',lambda:'proton-drive')
    monkeypatch.setattr(proton,'_run_cli_process',remote.process)
    return remote


@pytest.mark.parametrize('destination',[DESTINATION,'/my-files/CloudMover/'+READABLE+'/'+UID])
def test_ensure_all_destination_ancestors(remote,destination):
    parent,name=destination.rsplit('/',1)
    assert proton.ensure_folder(parent,name)==destination
    assert destination in remote.folders
    creates=[cmd[3:] for cmd in remote.calls if cmd[2]=='create-folder']
    if destination==DESTINATION:
        assert creates==[['/my-files/CloudMover',READABLE+' '+UID]]
    else:
        assert creates==[['/my-files/CloudMover',READABLE],['/my-files/CloudMover/'+READABLE,UID]]


@pytest.mark.parametrize('name',["Family photos - summer (2026)","O'Brien’s résumé 日本語"])
def test_paths_remain_single_argv_for_upload_verify_promote(remote,tmp_path,name):
    parent=proton.ensure_folder('/my-files/CloudMover',READABLE+' '+UID)
    parent=proton.ensure_folder(parent,name)
    local=tmp_path/(name+'.txt');local.write_text('disposable content')
    result=proton.safe_upload_promote(local,parent,local.name)
    proton.verify(local,parent)
    proton.verify_tree(tmp_path,parent,[local.name])
    assert result.name==local.name
    rename=next(c for c in remote.calls if c[2]=='rename')
    assert rename[-1]==local.name and rename[-2].startswith(parent+'/.cloudmover-upload-')
    upload=next(c for c in remote.calls if c[2]=='upload')
    assert upload[-1]==parent
    assert any(c==['proton-drive','filesystem','info',parent+'/'+local.name,'-j'] for c in remote.calls)
    assert any(c==['proton-drive','filesystem','list','-j','-t','file',parent] for c in remote.calls)


def test_failed_promotion_uses_verified_uid_cleanup(remote,tmp_path):
    parent=proton.ensure_folder('/my-files/CloudMover',READABLE+' '+UID)
    local=tmp_path/"O'Brien (copy) 日本語.txt";local.write_text('disposable')
    remote.fail_rename=True
    with pytest.raises(RuntimeError,match='Rename failed'):
        proton.safe_upload_promote(local,parent,local.name)
    assert ['proton-drive','filesystem','trash','/my-files/node-123'] in remote.calls
    assert not remote.files


def test_create_does_not_follow_auth_failure(remote,monkeypatch):
    def fail(cmd,**kw):return '', 'Authentication failed',1
    monkeypatch.setattr(proton,'_run_cli_process',fail)
    with pytest.raises(RuntimeError,match='Authentication failed'):
        proton.ensure_folder('/my-files/CloudMover','new name')
    assert not remote.calls


def planned(tmp_path,destination=DESTINATION):
    source=tmp_path/'SyncMeBro-2';source.mkdir()
    folder=source/"Folder (2026) - O'Brien 日本語";folder.mkdir()
    local=folder/'disposable file.txt';local.write_text('safe integration test')
    db=tmp_path/'migration.db'
    summary=state.initialize_migration(source,db,destination)
    c=state.connect(db)
    c.execute("UPDATE files SET selected=1,hydration_state='CLOUD_ONLY'")
    c.commit();c.close()
    return db,summary.migration_id,local


def run_engine(db,log):
    start.run(db,10,5_000_000_000,None,1,log,1,100_000_000,250,1,True)


@pytest.mark.parametrize("destination",[DESTINATION,"/my-files/SyncMeBro-2","/my-files"])
def test_disposable_file_hydrates_uploads_verifies_completes(remote,tmp_path,monkeypatch,destination):
    db,mid,local=planned(tmp_path,destination);log=tmp_path/'log.jsonl';downloaded=False;transitions=[]
    def inspect(path):
        return FileState(HydrationState.LOCAL if downloaded else HydrationState.CLOUD_ONLY,local.stat().st_size,local.stat().st_size if downloaded else 0)
    def download(path):
        nonlocal downloaded
        downloaded=True
    original=state.transition_file
    def transition(*args,**kwargs):
        transitions.append(args[3]);return original(*args,**kwargs)
    monkeypatch.setattr(hydrate,'inspect_file',inspect)
    monkeypatch.setattr(hydrate,'request_icloud_download',download)
    monkeypatch.setattr(hydrate,'transition_file',transition)
    monkeypatch.setattr(bulk_pipeline,'transition_file',transition)
    monkeypatch.setattr(start,'release_specific',lambda *args:(None,0))
    run_engine(db,log)
    assert [x.value for x in transitions]==['HYDRATING','READY','UPLOADING','VERIFYING','COMPLETE']
    c=state.connect(db);row=c.execute('SELECT transfer_state,verified,destination_path FROM files').fetchone();c.close()
    assert row['transfer_state']=='COMPLETE' and row['verified']==1
    assert row['destination_path'].startswith(destination+'/')
    assert destination in remote.folders and local.exists()
    events=[json.loads(line) for line in log.read_text().splitlines()]
    assert all(r['migration_id']==mid for r in events)
    timed=[r for r in events if r['event']=='safe_upload_stage']
    assert len(timed)==12
    assert all(r['relative_path']==str(local.relative_to(local.parents[1])) and r['logical_bytes']==local.stat().st_size and r['duration_ms']>=0 for r in timed)
    assert {'cycle_start','hydration_start','hydration_complete','upload_start','upload_complete','verification_start','verification_complete','release_start','release_complete','cycle_complete','migration_complete'} <= {r['event'] for r in events}


@pytest.mark.parametrize('stop',['error','reserve_stop','user_pause'])
def test_engine_stop_events_have_scope(remote,tmp_path,monkeypatch,stop):
    db,mid,local=planned(tmp_path);log=tmp_path/'log.jsonl'
    c=state.connect(db);c.execute("UPDATE files SET transfer_state='READY',hydration_state='LOCAL'");c.commit();c.close()
    if stop=='error':
        remote.fail_create=True
        with pytest.raises(SystemExit):run_engine(db,log)
    elif stop=='reserve_stop':
        monkeypatch.setattr(start,'free_bytes',lambda db:0)
        run_engine(db,log)
    else:
        def interrupt(*a,**kw):raise KeyboardInterrupt()
        monkeypatch.setattr(start,'run_bulk',interrupt)
        run_engine(db,log)
    records=[json.loads(line) for line in log.read_text().splitlines()]
    assert all(r['migration_id']==mid for r in records)
    assert records[-1]['event']==stop
    assert not start.engine_pid_path(db).exists()
    assert start.snapshot(db)['status']=='PAUSED'
    if stop=='error':
        assert 'create-folder' in MigrationEventReader(log).failure(mid)


def test_real_subprocess_boundary_receives_argv_without_shell(tmp_path,monkeypatch):
    backend=FakeProton();calls=[]
    class Process:
        returncode=0
        def __init__(self,cmd,**kwargs):
            assert not kwargs.get('shell',False)
            calls.append(cmd)
            self.out,self.err,self.returncode=backend.process(cmd)
        def communicate(self,**kwargs):return self.out,self.err
    monkeypatch.setattr(proton,'cli',lambda:'proton-drive')
    monkeypatch.setattr(proton.subprocess,'Popen',Process)
    name="O'Brien - photos (été) 日本語"
    path=proton.ensure_folder('/my-files/CloudMover',name)
    assert path=='/my-files/CloudMover/'+name
    assert ['proton-drive','filesystem','create-folder','/my-files/CloudMover',name] in calls
