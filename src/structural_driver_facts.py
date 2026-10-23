"""Backend-independent, bit-specific static driver evidence."""
from __future__ import annotations

DRIVER_CATEGORIES = frozenset({'multiple_driver', 'undriven_signal'})
DRIVER_DOMAIN = 'elaborated_static_drivers'
MAX_DRIVER_EVIDENCE = 32


def driver_fact(kind, path, bits, drivers, *, complete, source=None, net_kind=None):
    """Two positive writers need no exhaustive negative; zero writers do."""
    unique = {row['driver_id']: row for row in drivers}
    if kind == 'multiple_driver' and len(unique) < 2:
        raise ValueError('multiple-driver evidence needs two independent writers')
    if kind == 'undriven_signal' and (unique or not complete):
        raise ValueError('undriven evidence needs a complete empty driver inventory')
    if kind not in DRIVER_CATEGORIES or not bits:
        raise ValueError('invalid driver fact')
    instance, _, signal = path.rpartition('.')
    rows = list(unique.values())
    return dict(kind=kind, instance=instance, signal=signal, signal_path=path,
                target_bits=list(bits), source=source, net_kind=net_kind,
                origin='bit_driver_inventory', evidence_domain=DRIVER_DOMAIN,
                driver_count=len(rows), drivers=rows[:MAX_DRIVER_EVIDENCE],
                driver_evidence_truncated=len(rows) > MAX_DRIVER_EVIDENCE,
                driver_inventory_complete=bool(complete),
                driver_count_is_lower_bound=not complete,
                runtime_conflict_checked=False,
                interpretation=('Multiple known static writers; no runtime conflict verdict.'
                    if kind == 'multiple_driver' else
                    'No driver in the observed elaborated static model; runtime force/DPI/class writes are outside this proof.'))


def validate_driver_fact(fact):
    """Strict copied-fact shape for the remote native boundary."""
    bits, rows = fact.get('target_bits'), fact.get('drivers')
    if (not isinstance(bits, list) or not 1 <= len(bits) <= 4096
            or any(type(b) is not int for b in bits) or len(set(bits)) != len(bits)
            or not isinstance(rows, list) or len(rows) > MAX_DRIVER_EVIDENCE
            or any(not isinstance(r, dict) or not isinstance(r.get('driver_id'), str)
                   or not r['driver_id'] or len(r['driver_id']) > 16384 for r in rows)
            or len({r['driver_id'] for r in rows}) != len(rows)
            or type(fact.get('driver_count')) is not int
            or fact['driver_count'] < len(rows)
            or type(fact.get('driver_evidence_truncated')) is not bool
            or fact['driver_evidence_truncated'] != (fact['driver_count'] > len(rows))
            or type(fact.get('driver_inventory_complete')) is not bool
            or fact.get('driver_count_is_lower_bound') is not (not fact['driver_inventory_complete'])
            or fact.get('runtime_conflict_checked') is not False
            or fact.get('evidence_domain') != DRIVER_DOMAIN
            or fact.get('origin') != 'bit_driver_inventory'):
        raise ValueError('invalid static driver evidence')
    path = fact.get('signal_path')
    if (not isinstance(path, str) or path != fact.get('instance', '') + '.' + fact.get('signal', '')
            or len(path) > 16384):
        raise ValueError('invalid driver signal identity')
    if fact['kind'] == 'multiple_driver':
        if len(rows) < 2:
            raise ValueError('insufficient multiple-driver evidence')
    elif fact['kind'] == 'undriven_signal':
        if rows or fact['driver_count'] or not fact['driver_inventory_complete']:
            raise ValueError('unproved empty driver inventory')
    else:
        raise ValueError('unknown driver category')
    return fact
