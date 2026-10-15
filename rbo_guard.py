"""Legacy-writer boundary for RBO-owned catalog rows. No privileged write bypass.

Activation uses RBO_POLICY_PATH or the explicitly installed shared sidecar.
Missing policy is inert only for catalogs without an accepted RBO snapshot.
Configured invalid/missing policy fails closed. All catalog writers must
use the shared lock and optimistic snapshot digest to prevent lost updates.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import fcntl
import hashlib
import json
import os
import tempfile
import threading


class RBOGuardError(ValueError):
    pass


METADATA_FIELDS = frozenset({'description', 'photo', 'photos', 'image', 'images', 'photo_source', 'photo_updated_at'})
IDENTITY_FIELDS = ('article_1c', 'code_1c', 'article', 'art', 'guid_1c', 'ref_1c')
_local = threading.local()
DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / 'catalog_sync' / 'rbo_policy.json'


def _name(value):
    return ' '.join(str(value or '').casefold().split())


def _text(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RBOGuardError('policy identities must be nonempty trimmed strings')
    return value


@dataclass(frozen=True)
class Policy:
    enabled: bool
    mappings: tuple
    ids: frozenset
    codes: frozenset
    names: frozenset
    canonical: dict

    def canonical_id(self, product_id):
        return self.canonical.get(str(product_id), str(product_id)) if self.enabled else str(product_id)

    def owns(self, product):
        if not self.enabled:
            return False
        if isinstance(product, str):
            return product in self.ids
        if not isinstance(product, dict):
            return False
        return (any(str(product.get(k) or '') in self.ids for k in ('id', 'product_id'))
                or any(str(product.get(k) or '') in self.codes for k in IDENTITY_FIELDS)
                or any(_name(product.get(k)) in self.names for k in ('name', 'product_name', 'one_c_name')))

    def owns_action(self, action, registry=None):
        row = {'product_id':action.product_id, 'article':action.article, 'name':action.name}
        if self.owns(row):
            return True
        entries = getattr(registry, 'entries', {})
        for key in (action.article, *getattr(action, 'candidates', ())):
            entry = entries.get(key)
            if entry and self.owns({'product_id':entry.product_id, 'article':entry.article, 'name':entry.one_c_name}):
                return True
        return False

    def guard_catalog(self, old, new):
        if not self.enabled:
            return
        if old.get("_rbo_snapshot") != new.get("_rbo_snapshot"):
            raise RBOGuardError("RBO source snapshot metadata is read-only")
        before, after = _rows(old), _rows(new)
        for pid, row in before.items():
            if self.owns(row):
                if pid not in after:
                    raise RBOGuardError(f'RBO-owned product cannot be deleted: {pid}')
                left = {k:v for k,v in row.items() if k not in METADATA_FIELDS}
                right = {k:v for k,v in after[pid].items() if k not in METADATA_FIELDS}
                if left != right:
                    raise RBOGuardError(f'RBO-owned fields are read-only: {pid}')
        for pid, row in after.items():
            if self.owns(row) and (pid not in before or not self.owns(before[pid])):
                raise RBOGuardError(f'Legacy writer cannot create/relink RBO identity: {pid}')


def _rows(data):
    if not isinstance(data, dict) or not isinstance(data.get('products'), list):
        raise RBOGuardError('catalog must contain products list')
    rows = {}
    for row in data['products']:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or row['id'] in rows:
            raise RBOGuardError('catalog product IDs must be unique nonempty strings')
        rows[row['id']] = row
    return rows


def policy_path():
    """Environment wins; shared sidecar protects cron without service env vars."""
    if 'RBO_POLICY_PATH' in os.environ:
        return Path(_text(os.environ['RBO_POLICY_PATH']))
    path = Path(DEFAULT_POLICY_PATH)
    # A dangling configured symlink is also invalid, never silently disabled.
    return path if path.exists() or path.is_symlink() else None


def load_policy(catalog_path):
    configured = policy_path()
    if configured is None:
        try:
            catalog = json.loads(Path(catalog_path).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise RBOGuardError('catalog unavailable while resolving RBO policy') from exc
        if isinstance(catalog, dict) and '_rbo_snapshot' in catalog:
            raise RBOGuardError('policy missing for RBO catalog')
        return Policy(False, (), frozenset(), frozenset(), frozenset(), {})
    try:
        raw = json.loads(configured.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or raw.get('schema') != 1 or type(raw.get('enabled')) is not bool or not isinstance(raw.get('mappings'), list):
            raise RBOGuardError('invalid RBO policy schema')
        ids, codes, names, canonical = set(), set(), set(), {}
        for mapping in raw['mappings']:
            if not isinstance(mapping, dict):
                raise RBOGuardError('invalid policy mapping')
            primary = _text(mapping.get('primary_id'))
            code = _text(mapping.get('code_1c'))
            if code in codes:
                raise RBOGuardError('duplicate RBO source code')
            codes.add(code)
            owned = {primary}
            for key in ('alias_ids', 'selected_ids'):
                values = mapping.get(key, [])
                if not isinstance(values, list):
                    raise RBOGuardError('invalid policy ID list')
                owned.update(_text(value) for value in values)
            if ids & owned:
                raise RBOGuardError('duplicate RBO ownership ID')
            ids.update(owned)
            canonical.update({pid:primary for pid in owned})
            for key in ('guid_1c', 'ref_1c'):
                if mapping.get(key):
                    value = _text(mapping[key])
                    if value in codes:
                        raise RBOGuardError('duplicate RBO source identity')
                    codes.add(value)
            source_ids = mapping.get('source_identities', [])
            if not isinstance(source_ids, list):
                raise RBOGuardError('invalid source_identities')
            for value in source_ids:
                value = _text(value)
                if value in codes:
                    raise RBOGuardError('duplicate source identity')
                codes.add(value)
            source_names = mapping.get('source_names', [])
            if not isinstance(source_names, list):
                raise RBOGuardError('invalid source_names')
            names.update(_name(_text(value)) for value in source_names)
        # Exact known source names also prevent a legacy importer minting a new
        # ID when its spreadsheet omits codes. No fuzzy matching is used.
        catalog = json.loads(Path(catalog_path).read_text(encoding='utf-8'))
        for row in _rows(catalog).values():
            if row['id'] in ids or any(str(row.get(k) or '') in codes for k in IDENTITY_FIELDS):
                names.update(_name(row[k]) for k in ('name','product_name','one_c_name') if row.get(k))
        if raw['enabled'] and not ids:
            raise RBOGuardError('enabled policy has no owners')
        return Policy(raw['enabled'], tuple(raw['mappings']), frozenset(ids), frozenset(codes), frozenset(names), canonical)
    except (OSError, ValueError, TypeError) as exc:
        raise RBOGuardError(f'RBO policy unavailable/invalid: {exc}') from exc


@contextmanager
def catalog_lock(path):
    """Same lock inode as catalog_sync; nested calls in this thread are safe."""
    path = Path(path).resolve()
    lock = path.parent / f'.{path.name}.catalog-sync.lock'
    with file_lock(lock):
        yield


@contextmanager
def file_lock(lock):
    lock = Path(lock).resolve()
    key = str(lock)
    held = getattr(_local, 'held', None)
    if held is None:
        held = _local.held = set()
    if key in held:
        yield
        return
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_catalog(path):
    with catalog_lock(path):
        data = Path(path).read_bytes()
        return json.loads(data), hashlib.sha256(data).hexdigest()


def guarded_save(path, data, expected_digest):
    path = Path(path).resolve()
    with catalog_lock(path):
        # Recovery must run through catalog_sync with its complete target set.
        if (path.parent / '.catalog-sync-transaction.json').exists():
            raise RBOGuardError('pending catalog transaction requires recovery')
        old, digest = read_catalog(path)
        policy = load_policy(path)
        if policy.enabled and expected_digest is None:
            raise RBOGuardError('catalog snapshot digest required')
        if expected_digest is not None and expected_digest != digest:
            raise RBOGuardError('stale catalog snapshot; reload before saving')
        policy.guard_catalog(old, data)
        payload = (json.dumps(data, ensure_ascii=False, indent=2)+'\n').encode('utf-8')
        descriptor, temp = tempfile.mkstemp(prefix=f'.{path.name}.guard.', dir=path.parent)
        try:
            os.fchmod(descriptor, path.stat().st_mode & 0o777)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(temp): os.unlink(temp)
        return hashlib.sha256(payload).hexdigest()
