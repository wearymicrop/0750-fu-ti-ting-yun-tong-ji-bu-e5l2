import {$, cards, el, label, num, stamp, table} from './format.mjs';

const tones = {WRITTEN_OFF: 'written', PARTIAL: 'partial', NOT_WRITTEN_OFF: 'unwritten'};
const verdicts = {WRITTEN_OFF: '已核销', PARTIAL: '部分核销', NOT_WRITTEN_OFF: '未核销'};

function summarize(entries) {
  if (!entries.length) return null;
  const text = entries.map(entry => {
    const seconds = entry.seconds ? ` ${num(entry.seconds)} 秒` : '';
    return `${label(entry.code)}${seconds}`;
  }).join('；');
  const detail = entries.flatMap(entry => entry.segments.length
    ? entry.segments.map(segment =>
      `${label(entry.code)} · ${label(segment.code)} · ${stamp(segment.start)} → ${stamp(segment.end)}`)
    : [`${label(entry.code)} · 无覆盖时段`]);
  return {text, detail: detail.join('\n')};
}

function cell(entries) {
  const summary = summarize(entries);
  if (!summary) return el('span', '—');
  const node = el('span', summary.text);
  if (entries.some(entry => entry.segments.length)) {
    node.className = 'detail';
    node.title = summary.detail;
  }
  return node;
}

export function drawWriteoff(result) {
  if (!result.writeoff) {
    cards($('writeoff-kpis'), [['结论状态', '无核销留档', 'missing']]);
    $('writeoff-note').textContent = '该班报归档时没有核销结论，不能按现行规则改写历史证据。';
    table($('writeoff-table'), [['审批编号']], []);
    table($('writeoff-reasons'), [['未核销理由']], []);
    return false;
  }
  const {orders, summary} = result.writeoff;
  const state = summary.status;
  cards($('writeoff-kpis'), [
    ['结论状态', label(state), state === 'FORMAL' ? 'accent' : 'missing'],
    ['审批单', num(summary.orders)],
    ['已核销', num(summary.written_off), 'available'],
    ['部分核销', num(summary.partial), 'exempt'],
    ['未核销', num(summary.not_written_off), 'missing'],
    ['核销秒数', `${num(summary.written_seconds)} 秒`],
    ['对账豁免', `${num(summary.exempt_seconds)} 秒`],
  ]);
  const notes = {
    FORMAL: '逐单结论与对账豁免一致；冲突、缺证及高优原因占用的时段均未计入核销。',
    UNMATCHED: `逐单核销 ${num(summary.written_seconds)} 秒与对账豁免 ${num(summary.exempt_seconds)} 秒不一致，本次不形成正式结论。`,
    BLOCKED: `设备 ${summary.blocked_devices.join('、')} 四类秒数未对平，相关时刻已退回未核销，不得形成正式结论。`,
    NO_SERVICE: '统计窗口内没有营业时段，无法形成核销结论。',
  };
  $('writeoff-note').textContent = notes[state] ?? '核销结论状态未知，请复核。';

  table($('writeoff-table'),
    [['行号', true], ['审批编号'], ['设备'], ['审批原因'], ['状态'], ['结论'],
      ['覆盖', true], ['已核销', true], ['核销原因'], ['未核销', true], ['未核销理由'], ['未计入停运', true]],
    orders.map(order => [
      order.line, order.id ?? '—', order.device ?? '—', label(order.reason), label(order.status),
      el('span', verdicts[order.conclusion], `verdict ${tones[order.conclusion]}`),
      num(order.covered_seconds), num(order.written_seconds), cell(order.written),
      num(order.unwritten_seconds), cell(order.reasons), num(order.idle_seconds),
    ]));

  const reasons = Object.entries(summary.reason_seconds ?? {}).map(([code, seconds]) => [
    label(code), num(summary.reason_orders?.[code] ?? 0), num(seconds),
  ]);
  table($('writeoff-reasons'), [['未核销理由'], ['涉及审批单', true], ['秒数', true]], reasons);
  return true;
}
