from collections import defaultdict

from app.intervals import intersect, merge
from app.reasons import REASONS
from app.times import instant

WRITTEN_OFF = 'WRITTEN_OFF'
PARTIAL = 'PARTIAL'
NOT_WRITTEN_OFF = 'NOT_WRITTEN_OFF'

_WRITTEN, _UNWRITTEN, _IDLE = 'written', 'unwritten', 'idle'


def _classify(segment, permit):
    """判断审批覆盖的一小段营业时间能否计入正式核销。"""
    if segment['bucket'] == 'available':
        return _IDLE, 'NO_STOP'
    if segment['bucket'] == 'missing':
        # 冲突、未恢复、未知原因及采信时刻之后均属缺证，不可当作施工豁免。
        return _UNWRITTEN, segment['code']
    if permit['status'] != 'approved':
        return _UNWRITTEN, 'NOT_APPROVED'
    if permit['reason'] is None or permit['reason'] == segment['code']:
        return _WRITTEN, segment['code']
    if REASONS[segment['code']][0] < REASONS[permit['reason']][0]:
        return _UNWRITTEN, 'HIGHER_PRIORITY'
    return _UNWRITTEN, 'REASON_MISMATCH'


def _cover(start, end, segments):
    parts = []
    for segment in segments:
        common = intersect(segment['start'], segment['end'], start, end)
        if common is None:
            continue
        low, high = common
        parts.append({
            'start': low, 'end': high, 'seconds': high - low,
            'bucket': segment['bucket'], 'code': segment['code'],
            'winner_line': segment['winner_line'],
        })
    return parts


def _outside_reason(start, end, window):
    if intersect(start, end, window.start, window.end) is None:
        return 'OUTSIDE_WINDOW'
    return 'OUTSIDE_SERVICE'


def _conclude(permit, segments, window, balances):
    """逐张审批单拆分覆盖时段，返回结论和已核销区间。

    跨相邻报码时只把确实匹配审批原因、且已对平的确认停运计入核销；
    其余时段保留为未核销理由，整单不因局部冲突而作废。
    """
    device = permit['device']
    parts = _cover(permit['start'], permit['end'], segments.get(device, []))
    blocked = bool(balances.get(device, {}).get('difference'))
    written = unwritten = idle = 0
    grouped, credited, spans = {}, {}, []
    for part in parts:
        outcome, reason = _classify(part, permit)
        if blocked and outcome == _WRITTEN:
            # 该设备四类秒数尚未对平，相关时刻只能记为未核销。
            outcome, reason = _UNWRITTEN, 'DEVICE_UNBALANCED'
        part['outcome'], part['reason'] = outcome, reason
        if outcome == _WRITTEN:
            written += part['seconds']
            spans.append((part['start'], part['end']))
            entry = credited.setdefault(part['code'], {'code': part['code'], 'seconds': 0, 'segments': []})
            entry['seconds'] += part['seconds']
            entry['segments'].append({key: part[key] for key in ('start', 'end', 'code', 'winner_line')})
        elif outcome == _UNWRITTEN:
            unwritten += part['seconds']
            entry = grouped.setdefault(reason, {'code': reason, 'seconds': 0, 'segments': []})
            entry['seconds'] += part['seconds']
            entry['segments'].append({key: part[key] for key in ('start', 'end', 'code', 'winner_line')})
        else:
            idle += part['seconds']

    if written and not unwritten:
        conclusion = WRITTEN_OFF
    elif written:
        conclusion = PARTIAL
    else:
        conclusion = NOT_WRITTEN_OFF

    reasons = sorted(grouped.values(), key=lambda item: (item['segments'][0]['start'], item['code']))
    if not reasons and not written:
        code = _outside_reason(permit['start'], permit['end'], window) if not parts else 'NO_STOP'
        reasons = [{'code': code, 'seconds': idle, 'segments': []}]

    order = {
        'line': permit['line'], 'id': permit['id'], 'device': device,
        'reason': permit['reason'], 'status': permit['status'],
        'approval': (permit['raw'] or {}).get('approval'),
        'start': permit['start'], 'end': permit['end'],
        'permit_seconds': max(0, min(permit['end'], window.end) - max(permit['start'], window.start)),
        'covered_seconds': sum(part['seconds'] for part in parts),
        'outage_seconds': written + unwritten,
        'written_seconds': written, 'unwritten_seconds': unwritten, 'idle_seconds': idle,
        'conclusion': conclusion, 'blocked': blocked,
        'written': sorted(credited.values(), key=lambda item: item['code']),
        'reasons': reasons,
    }
    return order, spans


def _raw_time(raw, key):
    try:
        return instant(raw.get(key))
    except (TypeError, ValueError):
        return None


def _isolated_order(row, segments, window):
    """隔离的审批行同样逐单列出，未核销理由取隔离代码。"""
    raw = row['raw'] if isinstance(row['raw'], dict) else {}
    start, end = _raw_time(raw, 'start'), _raw_time(raw, 'end')
    if start is not None and end is not None and end <= start:
        start = end = None
    parts = (_cover(start, end, segments.get(raw.get('device')) or [])
             if start is not None and end is not None else [])
    unwritten = idle = 0
    grouped = {}
    for part in parts:
        if part['bucket'] == 'available':
            idle += part['seconds']
            continue
        unwritten += part['seconds']
        entry = grouped.setdefault(row['code'], {'code': row['code'], 'seconds': 0, 'segments': []})
        entry['seconds'] += part['seconds']
        entry['segments'].append({key: part[key] for key in ('start', 'end', 'code', 'winner_line')})
    if not grouped:
        grouped[row['code']] = {'code': row['code'], 'seconds': 0, 'segments': []}
    return {
        'line': row['line'], 'id': raw.get('id'), 'device': raw.get('device'),
        'reason': raw.get('reason'), 'status': raw.get('status'), 'approval': raw.get('approval'),
        'start': start, 'end': end,
        'permit_seconds': (max(0, min(end, window.end) - max(start, window.start))
                           if start is not None and end is not None else 0),
        'covered_seconds': sum(part['seconds'] for part in parts),
        'outage_seconds': unwritten, 'written_seconds': 0, 'unwritten_seconds': unwritten,
        'idle_seconds': idle, 'conclusion': NOT_WRITTEN_OFF, 'blocked': False,
        'written': [], 'reasons': sorted(grouped.values(), key=lambda item: item['code']),
    }


def conclude_writeoff(segments, permits, isolated, window, balance):
    by_device = defaultdict(list)
    for segment in segments:
        by_device[segment['device']].append(segment)
    ordered = {device: sorted(rows, key=lambda row: row['start']) for device, rows in by_device.items()}
    balances = balance['by_device']

    orders, written_spans = [], defaultdict(list)
    for permit in permits:
        order, spans = _conclude(permit, ordered, window, balances)
        orders.append(order)
        written_spans[order['device']].extend(spans)
    orders.extend(_isolated_order(row, ordered, window) for row in isolated)
    orders.sort(key=lambda order: order['line'])

    unique = {device: merge(spans) for device, spans in written_spans.items()}
    written_by_device = {device: sum(end - start for start, end in unique.get(device, []))
                         for device in sorted(balances)}
    blocked_devices = sorted(device for device, row in balances.items() if row['difference'])

    # 理由秒数按设备和理由取并集，重叠审批不重复计秒；单张审批的明细仍逐单保留。
    reason_spans = defaultdict(list)
    reason_orders = defaultdict(int)
    for order in orders:
        codes = {item['code'] for item in order['reasons']}
        for code in codes:
            reason_orders[code] += 1
        for reason in order['reasons']:
            for part in reason['segments']:
                reason_spans[reason['code'], order['device']].append((part['start'], part['end']))
    reason_seconds = defaultdict(int)
    for (code, _), spans in reason_spans.items():
        reason_seconds[code] += sum(end - start for start, end in merge(spans))
    for code in reason_orders:
        reason_seconds.setdefault(code, 0)

    written = sum(written_by_device.values())
    exempt = balance['totals']['exempt']
    if blocked_devices:
        state = 'BLOCKED'
    elif not balance['totals']['service']:
        state = 'NO_SERVICE'
    elif written != exempt:
        state = 'UNMATCHED'
    else:
        state = 'FORMAL'
    return {
        'orders': orders,
        'summary': {
            'status': state,
            'formal': state == 'FORMAL',
            'reconcile_status': balance['status'],
            'orders': len(orders),
            'written_off': sum(order['conclusion'] == WRITTEN_OFF for order in orders),
            'partial': sum(order['conclusion'] == PARTIAL for order in orders),
            'not_written_off': sum(order['conclusion'] == NOT_WRITTEN_OFF for order in orders),
            'unwritten_orders': sum(order['unwritten_seconds'] > 0 for order in orders),
            'written_seconds': written,
            'written_by_device': written_by_device,
            'exempt_seconds': exempt,
            'exempt_matches': written == exempt,
            'blocked_devices': blocked_devices,
            'reason_orders': dict(sorted(reason_orders.items())),
            'reason_seconds': dict(sorted(reason_seconds.items())),
        },
    }
