"""RBO order boundary. Pure validation never writes a catalog or submits an order."""
import json
import math
import os
import re
from pathlib import Path

CATALOG = Path(__file__).resolve().parent / 'tma_static' / 'products.json'


def current_policy():
    from rbo_guard import load_policy
    return load_policy(CATALOG)


def unit_key(value):
    text = re.sub(r'\s+', '', str(value or '').lower()).replace(',', '.')
    return re.sub(r'(\d+)\.0+(?=\D|$)', r'\1', text)


def prepare_items(items, products, policy):
    if not policy.enabled:
        return items
    catalog = {str(p['id']): p for p in products}
    out, merged, totals = [], {}, {}
    for source in items:
        if not isinstance(source, dict):
            raise ValueError('Некорректная позиция корзины')
        raw_id = str(source.get('id') or '')
        pid = policy.canonical_id(raw_id)
        if pid not in catalog:
            raise ValueError('Товар не найден: обновите корзину')
        if not policy.owns(raw_id):
            out.append({k:v for k,v in source.items() if not k.startswith("_")})
            continue
        product = catalog[pid]
        if product.get('hidden') or product.get('archived') or product.get('quarantined'):
            raise ValueError('Товар недоступен: обновите корзину')
        size = source.get('fasovka')
        packs = [p for p in product.get('fasovka', []) if unit_key(p.get('size')) == unit_key(size)]
        if len(packs) != 1:
            raise ValueError('Фасовка изменилась: удалите позицию и добавьте заново в нужных единицах')
        pack = packs[0]
        size = pack['size']
        try:
            qty, price = float(source.get('qty')), float(pack.get('price'))
            stock = float(product.get('stock'))
        except (TypeError, ValueError):
            raise ValueError('Цена, остаток или количество не подтверждены')
        if not all(math.isfinite(x) for x in (qty, price, stock)) or qty <= 0 or qty != int(qty):
            raise ValueError('Некорректное количество')
        if price <= 0 or pack.get('on_request') or product.get('on_request'):
            raise ValueError('Цена требует подтверждения')
        totals[pid] = totals.get(pid, 0) + qty
        if totals[pid] > stock:
            raise ValueError('Недостаточно остатка: обновите корзину')
        key = (pid, size, *(str(source.get(k) or '') for k in ('roast_choice', 'grind', 'variant')))
        if key in merged:
            merged[key]['qty'] += int(qty)
            continue
        line = {k: v for k, v in source.items() if not k.startswith('_')}
        line.update(id=pid, fasovka=size, name=product['name'], category=product.get('category', ''),
                    subcategory=product.get('subcategory', ''), price=price, qty=int(qty),
                    on_request=False, _rbo_base=True, _personal_priced=True)
        merged[key] = line
        out.append(line)
    return out


def prepare_payload(payload):
    policy = current_policy()
    if not policy.enabled:
        return payload
    from rbo_guard import read_catalog
    data, digest = read_catalog(CATALOG)
    products = data if isinstance(data, list) else data['products']
    result = dict(payload)
    result['items'] = prepare_items(payload.get('items', []), products, policy)
    from rbo_db import projection_digest
    result['_rbo_catalog_digest'] = projection_digest(data, policy)
    return result


def require_legacy_db_access(product_ids, con=None):
    policy = current_policy()
    if not policy.enabled:
        return
    if con is None:
        raise ValueError('RBO: используйте Mini App; корзина с числовыми ID требует проверки')
    from rbo_db import guard_legacy_ids
    guard_legacy_ids(con, product_ids, policy)


def external_identity(item, products, policy):
    """Resolve current canonical name-only clients without accepting legacy boxes."""
    if not policy.enabled: return None
    explicit = item.get('canonical_product_id') or item.get('product_id')
    if isinstance(explicit, str) and policy.owns(explicit):
        return explicit, item.get('unit')
    name = str(item.get('product_name') or '').strip()
    matches = [p for p in products if p.get('name') == name]
    primary_ids = {m['primary_id'] for m in policy.mappings}
    owned = [p for p in matches if p.get('id') in primary_ids]
    if owned:
        if len(matches) != 1 or len(owned) != 1 or len(owned[0].get('fasovka', [])) != 1:
            raise ValueError('Неоднозначный товар: обновите каталог или передайте canonical_product_id и unit')
        p = owned[0]
        return p['id'], item.get('unit') or p['fasovka'][0]['size']
    if policy.owns({'name': name}):
        raise ValueError('Старая карточка или фасовка: обновите каталог и повторите заказ')
    return None
