"""Exact, user-selected destination roots with internal ownership records."""
import json
from .providers import proton
from .state import connect, create_schema, get_latest_migration_id


def validate_destination(path):
    parts=path.split('/')
    if not path.startswith('/my-files/') or any(p in ('', '.', '..') for p in parts[1:]):
        raise RuntimeError('Choose a new destination folder inside Proton Drive.')


def preflight_destination(path):
    """Read-only: never adopt an existing node, even an empty folder."""
    validate_destination(path)
    try:
        proton.run(['filesystem','info',path],json_output=True)
    except proton.ProtonCLIError as exc:
        if not exc.stderr.strip().startswith('Node not found:'):
            raise
    else:
        raise RuntimeError('This Proton destination already exists. Choose a different new folder name; CloudMover will not merge into an existing folder.')
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
        if not row or row['destination'] != path:
            raise RuntimeError('Destination does not match the migration plan.')
        key='destination_owner:'+mid
        saved=c.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
        if saved:
            owner=json.loads(saved['value'])
            current=proton.info_file(path)
            if owner['path'] != path or not current.node_id or current.node_id != owner['uid']:
                raise RuntimeError('The Proton destination identity changed. Stopped without writing to it.')
        else:
            preflight_destination(path)
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
        c.commit()
        return path
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()
