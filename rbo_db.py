"""SQLite RBO projection and reservations, explicitly invoked by cutover tooling.

Never opens a database itself. The caller supplies its connection and controls
commit/rollback. A new accepted physical-stock snapshot replaces local checkout deductions,
as in the legacy import. Replaying the same digest preserves deductions.
"""
import json
import math


def projection_digest(data, policy):
    """Accepted source version + owned trading fields, independent of metadata."""
    import hashlib
    version=(data.get('_rbo_snapshot') or {}).get('source_id')
    if not isinstance(version,str) or not version:
        raise ValueError('RBO source version missing')
    primaries={m['primary_id'] for m in policy.mappings}
    rows=[{'id':p['id'],'stock':p.get('stock'),
           'packs':[{'size':f.get('size'),'price':f.get('price'),'on_request':f.get('on_request',False)} for f in p.get('fasovka',[])]}
          for p in data['products'] if p['id'] in primaries]
    if len(rows)!=len(primaries): raise ValueError('RBO projection incomplete')
    raw=json.dumps({'source_version':version,'products':sorted(rows,key=lambda x:x['id'])},sort_keys=True,ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def schema(con):
    con.execute('''CREATE TABLE IF NOT EXISTS rbo_products (
        canonical_id TEXT PRIMARY KEY, product_id INTEGER UNIQUE NOT NULL,
        catalog_digest TEXT NOT NULL, price REAL NOT NULL, source_stock REAL NOT NULL,
        reserved REAL NOT NULL DEFAULT 0, unit TEXT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS rbo_db_aliases (
        product_id INTEGER PRIMARY KEY, canonical_id TEXT NOT NULL)''')


def stage_snapshot(con, products, policy, digest, explicit_bindings=None):
    """Apply all owned primary rows under caller's SQLite transaction.

    explicit_bindings is reviewed canonical ID -> existing numeric DB ID. Exact
    unique full-name matches can be used; ambiguous names get a distinct new row.
    No commits here; invoke inside `with con:` and BEGIN IMMEDIATE.
    """
    schema(con)
    bindings = explicit_bindings or {}
    primaries = {m['primary_id'] for m in policy.mappings}
    rows = {p['id']:p for p in products}
    if not primaries <= rows.keys():
        raise ValueError('RBO DB snapshot incomplete')
    for canonical in sorted(primaries):
        p=rows[canonical]; packs=p.get('fasovka',[])
        if len(packs)!=1:
            raise ValueError('RBO DB needs one exact unit per canonical ID')
        price=float(packs[0]['price']); stock=float(p['stock']); unit=str(packs[0]['size'])
        if not all(math.isfinite(x) for x in (price,stock)) or price<=0 or stock<0:
            raise ValueError('Invalid RBO price/stock')
        existing=con.execute('SELECT product_id FROM rbo_products WHERE canonical_id=?',(canonical,)).fetchone()
        pid=existing[0] if existing else bindings.get(canonical)
        if pid is None:
            candidates=con.execute('SELECT id FROM products WHERE name=?',(p['name'],)).fetchall()
            if len(candidates)==1 and not con.execute('SELECT 1 FROM rbo_products WHERE product_id=?',(candidates[0][0],)).fetchone():
                pid=candidates[0][0]
        if pid is None:
            pid=con.execute('INSERT INTO products(name,description,price,stock) VALUES(?,?,?,?)',
                (p['name'],'RBO canonical: '+canonical,price,stock)).lastrowid
        if not con.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone():
            raise ValueError('Explicit RBO DB binding does not exist')
        con.execute('''INSERT INTO rbo_products(canonical_id,product_id,catalog_digest,price,source_stock,reserved,unit)
            VALUES(?,?,?,?,?,0,?) ON CONFLICT(canonical_id) DO UPDATE SET
            reserved=CASE WHEN rbo_products.catalog_digest=excluded.catalog_digest THEN rbo_products.reserved ELSE 0 END,
            catalog_digest=excluded.catalog_digest,price=excluded.price,source_stock=excluded.source_stock,unit=excluded.unit''',
            (canonical,pid,digest,price,stock,unit))
        con.execute('UPDATE products SET price=?,stock=(SELECT source_stock-reserved FROM rbo_products WHERE canonical_id=?) WHERE id=?',(price,canonical,pid))
    return {r[0]:r[1] for r in con.execute('SELECT canonical_id,product_id FROM rbo_products')}


def resolve(con, canonical, digest):
    try:
        row=con.execute('SELECT product_id,price,source_stock,reserved,unit,catalog_digest FROM rbo_products WHERE canonical_id=?',(canonical,)).fetchone()
    except Exception as e:
        raise ValueError('RBO DB mapping not installed') from e
    if row is None or row[5]!=digest:
        raise ValueError('RBO DB and catalog versions differ')
    return dict(product_id=row[0],price=row[1],stock=row[2]-row[3],unit=row[4])


def reserve_items(con, items, digest):
    """Validate every line then reserve aggregate quantities atomically in caller txn."""
    totals={};resolved={}
    for it in items:
        if not it.get('_rbo_base'): continue
        canonical=it['id'];row=resolve(con,canonical,digest)
        if row['unit']!=it['fasovka'] or row['price']!=it['price']:
            raise ValueError('RBO unit/price version mismatch')
        totals[canonical]=totals.get(canonical,0)+it['qty'];resolved[canonical]=row
    for canonical,qty in totals.items():
        cur=con.execute('UPDATE rbo_products SET reserved=reserved+? WHERE canonical_id=? AND source_stock-reserved>=?',(qty,canonical,qty))
        if cur.rowcount!=1: raise ValueError('Недостаточно остатка RBO')
        con.execute('UPDATE products SET stock=stock-? WHERE id=?',(qty,resolved[canonical]['product_id']))
    for it in items:
        if it.get('_rbo_base'): it['_rbo_db_id']=resolved[it['id']]['product_id']
    return resolved


def guard_legacy_ids(con, product_ids, policy):
    if not policy.enabled: return
    # Every legacy row must be classified by the installed projection or exact
    # reviewed alias binding. Unknown numeric IDs cannot silently bypass ownership.
    schema(con)
    for pid in product_ids:
        if con.execute('SELECT 1 FROM rbo_products WHERE product_id=?',(pid,)).fetchone() or con.execute('SELECT 1 FROM rbo_db_aliases WHERE product_id=?',(pid,)).fetchone():
            raise ValueError('RBO DB row is read-only to legacy writers')
        row=con.execute('SELECT name FROM products WHERE id=?',(pid,)).fetchone()
        if row and policy.owns({'name':row[0]}):
            raise ValueError('RBO legacy DB identity requires explicit binding')


class DeferredCommit:
    """Keep legacy helpers' intermediate commits inside the enclosing order txn."""
    def __init__(self, connection): self.connection=connection
    def __getattr__(self, key): return getattr(self.connection,key)
    def commit(self): pass


def sync_products(con, products, policy, digest):
    """Return /sync products from one accepted version; hide legacy RBO duplicates."""
    if not policy.enabled:
        return [dict(r) for r in con.execute('SELECT name,price,stock FROM products ORDER BY name')]
    mapped={}
    for p in products:
        if policy.owns(p['id']) and policy.canonical_id(p['id'])==p['id']:
            mapped[p['id']]=dict(resolve(con,p['id'],digest),name=p['name'])
    result=[]
    mapped_db_ids={p['product_id'] for p in mapped.values()}
    for row in con.execute('SELECT id,name,price,stock FROM products ORDER BY name'):
        r=dict(row)
        if r['id'] in mapped_db_ids or policy.owns({'name':r['name']}): continue
        result.append({k:r[k] for k in ('name','price','stock')})
    for canonical,p in mapped.items():
        result.append(dict(id=canonical,name=p['name'],price=p['price'],stock=p['stock'],unit=p['unit'],catalog_digest=digest))
    return result


def stage_current_database(con):
    """Boot hook after legacy schema initialization, before order handlers register."""
    from rbo_cart import CATALOG, current_policy
    from rbo_guard import catalog_lock, read_catalog
    policy=current_policy()
    if not policy.enabled: return {}
    with catalog_lock(CATALOG):
        data,_=read_catalog(CATALOG)
        with con:
            con.execute('BEGIN IMMEDIATE')
            return stage_snapshot(con,data['products'],policy,projection_digest(data,policy))


def main():
    import argparse, hashlib, os, sqlite3
    from pathlib import Path
    from rbo_guard import load_policy, catalog_lock
    parser=argparse.ArgumentParser(description='Prepare/explicitly apply an RBO SQLite projection')
    parser.add_argument('--db',required=True)
    parser.add_argument('--catalog',required=True)
    parser.add_argument('--policy',required=True)
    parser.add_argument('--expected-digest',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--owner-approved',action='store_true')
    args=parser.parse_args()
    if args.apply and not args.owner_approved: parser.error('--apply requires --owner-approved')
    catalog=Path(args.catalog).resolve()
    os.environ['RBO_POLICY_PATH']=str(Path(args.policy).resolve())
    with catalog_lock(catalog):
        raw=catalog.read_bytes();digest=hashlib.sha256(raw).hexdigest()
        if digest!=args.expected_digest: raise ValueError('Catalog digest changed')
        data=json.loads(raw);policy=load_policy(catalog)
        if args.apply and not policy.enabled: raise ValueError('Applying DB snapshot requires enabled approved policy')
        if args.apply:
            con=sqlite3.connect(str(Path(args.db).resolve()))
        else:
            source=sqlite3.connect(Path(args.db).resolve().as_uri()+'?mode=ro',uri=True)
            con=sqlite3.connect(':memory:');source.backup(con);source.close()
        try:
            with con:
                con.execute('BEGIN IMMEDIATE')
                bindings=stage_snapshot(con,data['products'],policy,projection_digest(data,policy))
            print(json.dumps({'applied':args.apply,'catalog_digest':digest,'projection_digest':projection_digest(data,policy),'canonical_products':len(bindings),'bindings':bindings},ensure_ascii=False))
        finally: con.close()


if __name__=='__main__': main()
