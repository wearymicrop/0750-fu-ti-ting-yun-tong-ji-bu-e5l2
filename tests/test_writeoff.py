from app.modules.writeoff.conclude import conclude_writeoff
from app.pipeline import evaluate

# 跨相邻报码的审批：SAFETY 22:00-22:30、POWER 22:30-23:00、MECHANICAL 23:00-23:30，
# 单张 SAFETY 审批 22:00-23:30 覆盖三段。

def spanning():
    return {
        'start': '2026-11-19T20:00:00+08:00', 'end': '2026-11-20T06:00:00+08:00',
        'cutoff': '2026-11-20T06:00:00+08:00',
        'devices': [{'device': '001', 'name': '东厅'}],
        'shifts': [{'device': '001', 'day': '2026-11-19', 'from': '21:00:00', 'to': '02:00:00'}],
        'events': [
            {'device': '001', 'id': 'E1', 'start': '2026-11-19T22:00:00+08:00',
             'end': '2026-11-19T22:30:00+08:00', 'reason': 'SAFETY'},
            {'device': '001', 'id': 'E2', 'start': '2026-11-19T22:30:00+08:00',
             'end': '2026-11-19T23:00:00+08:00', 'reason': 'POWER'},
            {'device': '001', 'id': 'E3', 'start': '2026-11-19T23:00:00+08:00',
             'end': '2026-11-19T23:30:00+08:00', 'reason': 'MECHANICAL'},
        ],
        'permits': [
            {'device': '001', 'id': 'P1', 'start': '2026-11-19T22:00:00+08:00',
             'end': '2026-11-19T23:30:00+08:00', 'reason': 'SAFETY', 'status': 'approved',
             'approval': '施工单 1'},
        ],
    }


def order_of(result, report_id):
    return next(order for order in result['writeoff']['orders'] if order['id'] == report_id)


def test_spanning_approval_partial_not_voided():
    result = evaluate(spanning())
    order = order_of(result, 'P1')
    assert order['conclusion'] == 'PARTIAL'
    assert (order['written_seconds'], order['unwritten_seconds']) == (1800, 3600)
    assert [reason['code'] for reason in order['reasons']] == ['REASON_MISMATCH']
    assert all(segment['code'] in {'POWER', 'MECHANICAL'} for segment in order['reasons'][0]['segments'])
    # 冲突与不匹配的时段不计入核销，逐单结论与对账豁免一致。
    assert result['writeoff']['summary']['written_seconds'] == 1800
    assert result['writeoff']['summary']['exempt_seconds'] == 1800


def test_higher_priority_reason_cannot_be_exempted(dirty):
    result = evaluate(dirty)
    order = order_of(result, 'P-103')
    assert order['conclusion'] == 'NOT_WRITTEN_OFF'
    assert order['written_seconds'] == 0 and order['unwritten_seconds'] == 4500
    assert order['reasons'][0]['code'] == 'HIGHER_PRIORITY'


def test_conflict_and_open_event_are_never_exempt(dirty):
    result = evaluate(dirty)
    # 为缺证时段补两张覆盖全设备的批准单，冲突与未恢复仍不得核销。
    dirty['permits'].append({'device': '001', 'id': 'P-999', 'reason': None, 'status': 'approved',
                             'approval': '总单', 'start': '2026-11-20T03:00:00+08:00',
                             'end': '2026-11-20T04:00:00+08:00'})
    dirty['permits'].append({'device': '002', 'id': 'P-998', 'reason': None, 'status': 'approved',
                             'approval': '总单二', 'start': '2026-11-20T00:00:00+08:00',
                             'end': '2026-11-20T00:30:00+08:00'})
    result = evaluate(dirty)
    open_event = order_of(result, 'P-999')
    assert open_event['conclusion'] == 'NOT_WRITTEN_OFF'
    assert open_event['written_seconds'] == 0 and open_event['unwritten_seconds'] == 3600
    assert open_event['reasons'][0]['code'] == 'OPEN_EVENT'
    conflict = order_of(result, 'P-998')
    assert conflict['conclusion'] == 'NOT_WRITTEN_OFF'
    assert conflict['written_seconds'] == 0 and conflict['unwritten_seconds'] == 1800
    assert conflict['reasons'][0]['code'] == 'EVENT_CONFLICT'
    assert not any(segment['bucket'] == 'exempt' and segment['code'] in {'EVENT_CONFLICT', 'OPEN_EVENT'}
                   for segment in result['segments'])


def test_pending_and_missing_approval_are_unwritten(dirty):
    result = evaluate(dirty)
    pending = order_of(result, 'P-202')
    assert pending['conclusion'] == 'NOT_WRITTEN_OFF'
    assert pending['reasons'][0]['code'] == 'NOT_APPROVED'
    missing = next(order for order in result['writeoff']['orders'] if order['line'] == 6)
    assert missing['conclusion'] == 'NOT_WRITTEN_OFF'
    assert missing['reasons'][0]['code'] == 'MISSING_APPROVAL'


def test_every_permit_row_appears_once(dirty):
    result = evaluate(dirty)
    lines = [order['line'] for order in result['writeoff']['orders']]
    assert lines == [1, 2, 3, 4, 5, 6]
    assert result['writeoff']['summary']['orders'] == 6


def test_overlapping_orders_count_seconds_once(dirty):
    result = evaluate(dirty)
    summary = result['writeoff']['summary']
    # 第 1、2 行在 22:30–22:45 交叠，逐单相加为 4,500 秒，正式核销按并集取 3,600 秒。
    assert [order_of(result, key)['written_seconds'] for key in ('P-101', 'P-102')] == [2700, 1800]
    assert summary['written_by_device'] == {'001': 3600, '002': 900}
    assert summary['written_seconds'] == summary['exempt_seconds'] == 4500
    assert summary['status'] == 'FORMAL'


def test_written_spans_equal_exempt_buckets(dirty):
    from app.intervals import merge

    result = evaluate(dirty)
    exempt, written = {}, {}
    for segment in result['segments']:
        if segment['bucket'] == 'exempt':
            exempt.setdefault(segment['device'], []).append((segment['start'], segment['end']))
    for order in result['writeoff']['orders']:
        for item in order['written']:
            for part in item['segments']:
                written.setdefault(order['device'], []).append((part['start'], part['end']))
    for device in sorted(exempt):
        assert merge(exempt[device]) == merge(written.get(device, []))


def test_conclusion_is_independent_of_input_order(dirty):
    def snapshot(result):
        orders = {order['id']: (order['conclusion'], order['written_seconds'], order['unwritten_seconds'],
                                order['idle_seconds'],
                                [(reason['code'], reason['seconds']) for reason in order['reasons']])
                  for order in result['writeoff']['orders']}
        return orders, result['writeoff']['summary']

    prior = snapshot(evaluate(dirty))
    dirty['permits'].reverse()
    dirty['events'].reverse()
    assert snapshot(evaluate(dirty)) == prior


def test_mismatched_exempt_total_is_not_formal():
    result = evaluate(spanning())
    balance = result['reconcile']
    balance['totals']['exempt'] = balance['totals']['exempt'] + 60
    writeoff = conclude_writeoff(result['attributed'], result['permits'], result['permit_issues'],
                                 Window(**result['window']), balance)
    assert writeoff['summary']['status'] == 'UNMATCHED'
    assert writeoff['summary']['formal'] is False
    assert writeoff['summary']['written_seconds'] == 1800


def test_devices_without_orders_are_summarized_as_zero():
    data = spanning()
    data['devices'].append({'device': '002', 'name': '西桥'})
    data['shifts'].append({'device': '002', 'day': '2026-11-19', 'from': '22:00:00', 'to': '23:00:00'})
    result = evaluate(data)
    assert result['writeoff']['summary']['written_by_device'] == {'001': 1800, '002': 0}
    assert result['writeoff']['summary']['status'] == 'FORMAL'


def test_unbalanced_device_blocks_formal_conclusion():
    result = evaluate(spanning())
    balance = result['reconcile']
    balance['by_device']['001']['difference'] = 10
    writeoff = conclude_writeoff(result['attributed'], result['permits'], result['permit_issues'],
                                 Window(**result['window']), balance)
    order = writeoff['orders'][0]
    assert order['blocked'] and order['written_seconds'] == 0 and order['unwritten_seconds'] == 5400
    assert order['reasons'][0]['code'] == 'DEVICE_UNBALANCED'
    assert writeoff['summary']['status'] == 'BLOCKED' and writeoff['summary']['formal'] is False


class Window:
    def __init__(self, start, end, cutoff):
        self.start, self.end, self.cutoff = start, end, cutoff
