from fastapi import APIRouter
from fastapi.responses import Response

from app.config import RULE_VERSION
from app.errors import BusinessError
from app.modules.reports.manifest import canonical
from app.modules.reports.store import fetch
from app.pipeline import evaluate, sample_stage
from app.samples import load_sample

router = APIRouter(prefix='/api', tags=['审批核销'])


def archived_writeoff(report_id):
    snapshot = fetch(report_id)[1]
    if 'writeoff' not in snapshot['result']:
        # 旧版班报没有核销结论，不得用当前规则重算替换留档证据。
        raise BusinessError('WRITEOFF_NOT_ARCHIVED', '该班报归档时没有核销结论，请勿重算覆盖', 409)
    return snapshot


@router.get('/writeoff/{sample}', summary='审批核销')
def writeoff(sample: str):
    return sample_stage(sample, 'writeoff')


@router.get('/writeoff/{sample}/export', summary='下载样例核销结论')
def export_sample(sample: str):
    result = evaluate(load_sample(sample))
    envelope = {
        'source': sample, 'report_id': None, 'rule_version': RULE_VERSION,
        'window': result['window'], 'writeoff': result['writeoff'],
    }
    return Response(canonical(envelope) + '\n', media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="lift-writeoff-{sample}.json"'})


@router.get('/runs/{report_id}/writeoff', summary='读取归档核销结论')
def report_writeoff(report_id: str):
    return archived_writeoff(report_id)['result']['writeoff']


@router.get('/runs/{report_id}/writeoff/export', summary='下载归档核销结论')
def export_report(report_id: str):
    snapshot = archived_writeoff(report_id)
    envelope = {
        'source': snapshot['manifest']['source'], 'report_id': snapshot['id'],
        'rule_version': snapshot['manifest']['rule_version'], 'digest': snapshot['manifest']['digest'],
        'window': snapshot['result']['window'], 'writeoff': snapshot['result']['writeoff'],
    }
    return Response(canonical(envelope) + '\n', media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="lift-writeoff-{snapshot["id"][:12]}.json"'})
