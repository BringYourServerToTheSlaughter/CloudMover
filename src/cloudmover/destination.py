"""Exact, user-selected destination roots with internal ownership records."""
import json
from pathlib import PurePosixPath
from .providers import proton
from .state import connect, create_schema, get_latest_migration_id


class DestinationExistsError(RuntimeError):
    def __init__(self,path):
        self.path=path
        super().__init__(f'This Proton destination already exists: {path}. Choose a different new folder name; CloudMover will not merge into an existing folder.')


def validate_destination(path):
    if path in ('/my-files', '/my-files/'):
        return
    parts=path.split('/')
    if not path.startswith('/my-files/') or any(p in ('', '.', '..') for p in parts[1:]):
        raise RuntimeError('Choose a new destination folder inside Proton Drive.')


def preflight_destination(path):
    """Read-only: never adopt an existing node, even an empty folder."""
    validate_destination(path)
    if path in ('/my-files', '/my-files/'):
        proton.run(['filesystem','info','/my-files'],json_output=True)
        return
    try:
        proton.run(['filesystem','info',path],json_output=True)
    except proton.ProtonCLIError as exc:
        if not exc.stderr.strip().startswith('Node not found:'):
            raise
    else:
        raise DestinationExistsError(path)
    parent=path.rsplit('/',1)[0]
    proton.run(['filesystem','info',parent],json_output=True)


def prepare_destination(db, path, migration_id=None):
    """Create exactly the selected root, or validate this migration's recorded UID.

    A crash between remote creation and the local commit fails closed on retry:
    the orphan folder is preserved and must not be adopted implicitly.
    """
    validate_destination(path)
    c=connect(db)
    try:
        create_schema(c)
        c.execute('BEGIN IMMEDIATE')
        mid=migration_id or get_latest_migration_id(c)
        row=c.execute('SELECT destination FROM migrations WHERE id=?',(mid,)).fetchone()
        if not row or row['destination'].rstrip('/') != path.rstrip('/'):
            raise RuntimeError('Destination does not match the migration plan.')
        path=path.rstrip('/')
        key='destination_owner:'+mid
        saved=c.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        if saved:
            owner=json.loads(saved['value'])
            current=proton.info_file(path)
            if owner['path'] != path or not current.node_id or current.node_id != owner['uid']:
                raise RuntimeError('The Proton destination identity changed. Stopped without writing to it.')
        else:
            preflight_destination(path)
            if path == '/my-files':
                # The user explicitly selected the existing Drive root. Record its
                # identity; normal per-file collision and verification gates remain.
                current=proton.info_file(path)
                if not current.node_id:
                    raise RuntimeError('Could not prove the Proton Drive root identity.')
                c.execute('INSERT INTO metadata(key,value) VALUES (?,?)',
                          (key,json.dumps({'path':path,'uid':current.node_id})))
            else:
                _create_owned_folder(c,key,path)
        if path == '/my-files':
            _prepare_root_children(c,mid)
        c.commit()
        return path
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def _create_owned_folder(c,key,path):
    parent,name=path.rsplit('/',1)
    created=proton._unwrap(json.loads(proton.run(['filesystem','create-folder',parent,name],json_output=True)))
    uid=created.get('uid') if isinstance(created,dict) else None
    if not uid:
        raise RuntimeError('Proton did not return the created folder identity. The folder has been preserved; choose a new destination.')
    current=proton.info_file(path)
    if current.node_id != uid or current.name != name:
        raise RuntimeError('Could not prove ownership of the new Proton destination. Stopped without uploading.')
    c.execute('INSERT INTO metadata(key,value) VALUES (?,?)',
              (key,json.dumps({'path':path,'uid':uid})))


def _prepare_root_children(c,mid):
    """Root is shared; only migration-owned child folders may be reused.

    Check all selected namespaces before creating any folders. Ownership is
    committed with the root record; interrupted creation leaves a refused orphan.
    Existing final-file checks at promotion remain necessary and unchanged.
    """
    rows=c.execute('SELECT relative_path,transfer_state FROM files WHERE migration_id=? AND selected=1',(mid,)).fetchall()
    folders=set()
    for row in rows:
        rel=PurePosixPath(row['relative_path'])
        folders.update('/my-files/'+str(p) for p in rel.parents if str(p)!='.')
    pending=[]
    for path in sorted(folders,key=lambda p:(p.count('/'),p)):
        key='destination_child_owner:'+mid+':'+path
        saved=c.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        if saved:
            owner=json.loads(saved['value']);current=proton.info_file(path)
            if owner['path']!=path or not current.node_id or current.node_id!=owner['uid']:
                raise RuntimeError('The Proton destination folder identity changed. Stopped without writing to it.')
        else:
            try:
                proton.run(['filesystem','info',path],json_output=True)
            except proton.ProtonCLIError as exc:
                if not exc.stderr.strip().startswith('Node not found:'):
                    raise
            else:
                raise DestinationExistsError(path)
            pending.append((key,path))
    for row in rows:
        if '/' not in row['relative_path'] and row['transfer_state']!='COMPLETE':
            path='/my-files/'+row['relative_path']
            try:
                proton.run(['filesystem','info',path],json_output=True)
            except proton.ProtonCLIError as exc:
                if not exc.stderr.strip().startswith('Node not found:'):
                    raise
            else:
                raise DestinationExistsError(path)
    for key,path in pending:
        _create_owned_folder(c,key,path)
