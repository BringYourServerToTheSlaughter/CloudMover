"""Read migration-scoped JSONL outcomes without changing durable migration state."""
import json
from pathlib import Path


class MigrationEventReader:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.identity = None
        self.errors = {}
        self.legacy_migration = None

    def failure(self, migration_id):
        if not migration_id:
            return None
        try:
            with self.path.open('rb') as stream:
                st = self.path.stat()
                identity = (st.st_dev, st.st_ino)
                if identity != self.identity or st.st_size < self.offset:
                    self.offset = 0
                    self.errors.clear()
                    self.legacy_migration = None
                self.identity = identity
                stream.seek(self.offset)
                for line in stream:
                    if not line.endswith(b'\n'):
                        break  # Writer has not finished this record yet.
                    self.offset += len(line)
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    event = record.get('event')
                    mid = record.get('migration_id')
                    if event == 'start':
                        self.legacy_migration = mid
                    # Older engine events omitted migration_id after a scoped start.
                    mid = mid or self.legacy_migration
                    if not mid:
                        continue
                    if event in ('start', 'migration_complete'):
                        self.errors.pop(mid, None)
                    elif event == 'error':
                        self.errors[mid] = str(record.get('error') or 'The migration engine stopped unexpectedly.')
        except OSError:
            pass
        return self.errors.get(migration_id)


def friendly_engine_failure(details):
    if 'create-folder' in details:
        prefix = 'CloudMover could not create the Proton destination folder.'
    else:
        prefix = 'The migration engine stopped.'
    if 'Node not found:' in details:
        reason = 'Proton could not find a required parent folder.'
    elif 'stderr:' in details:
        reason = details.split('stderr:', 1)[1].split(' | stdout:', 1)[0].strip()[:220]
    else:
        reason = details[:220] if 'running:' not in details else 'Proton could not complete the requested operation.'
    return f'{prefix} {reason} Your iCloud originals remain safe. Check the error details, then resume.'
