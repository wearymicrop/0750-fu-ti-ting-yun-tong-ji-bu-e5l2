import json

from app.modules.reports.manifest import sha256


def test_page_and_download_agree(client):
    page = client.get('/api/writeoff/dirty').json()
    response = client.get('/api/writeoff/dirty/export')
    attachment = response.json()
    assert response.headers['content-disposition'].startswith('attachment;')
    assert attachment['writeoff'] == page
    assert attachment['source'] == 'dirty' and attachment['report_id'] is None


def test_archived_download_matches_page_and_archive(client):
    report = client.post('/api/runs', json={'sample': 'dirty'}).json()
    page = client.get('/api/writeoff/dirty').json()
    archived = client.get(f"/api/runs/{report['id']}/writeoff").json()
    attachment = client.get(f"/api/runs/{report['id']}/writeoff/export").json()
    assert archived == page == attachment['writeoff']
    assert attachment['report_id'] == report['id']
    assert attachment['digest'] == report['manifest']['digest']


def test_rerun_keeps_conclusion_and_evidence(client):
    report = client.post('/api/runs', json={'sample': 'dirty'}).json()
    before = client.get(f"/api/runs/{report['id']}").json()['result']['writeoff']
    again = client.post(f"/api/runs/{report['id']}/rerun", json={}).json()
    assert again['reused'] is True
    after = again['result']['writeoff']
    assert before == after
    assert after['summary']['written_seconds'] == after['summary']['exempt_seconds']


def test_rerun_detects_changed_conclusion(client, monkeypatch):
    import app.modules.reports.routes as routes

    report = client.post('/api/runs', json={'sample': 'dirty'}).json()
    original = routes.build_report

    def tampered(source, data):
        fresh = original(source, data)
        fresh['result']['writeoff']['summary']['written_seconds'] += 1
        fresh['manifest']['digest'] = sha256(fresh['result'])
        return fresh

    monkeypatch.setattr(routes, 'build_report', tampered)
    response = client.post(f"/api/runs/{report['id']}/rerun", json={})
    assert response.status_code == 409 and response.json()['code'] == 'RERUN_MISMATCH'


def test_legacy_report_without_writeoff_is_not_reinterpreted(client):
    report = client.post('/api/runs', json={'sample': 'dirty'}).json()
    from app.modules.reports.store import database, canonical

    legacy = {**report}
    legacy['result'] = {key: value for key, value in report['result'].items() if key != 'writeoff'}
    with database() as connection:
        connection.execute('UPDATE reports SET snapshot_json = ? WHERE id = ?',
                           (canonical(legacy), report['id']))
    for response in (client.get(f"/api/runs/{report['id']}/writeoff"),
                     client.get(f"/api/runs/{report['id']}/writeoff/export"),
                     client.post(f"/api/runs/{report['id']}/rerun", json={})):
        assert response.status_code == 409 and response.json()['code'] == 'WRITEOFF_NOT_ARCHIVED'


def test_custom_batch_is_concluded_and_downloaded(client):
    from app.samples import load_sample

    data = load_sample('dirty')
    report = client.post('/api/runs', json={'data': data}).json()
    assert report['result']['writeoff']['summary']['orders'] == 6
    exported = client.get(f"/api/runs/{report['id']}/writeoff/export").json()
    assert exported['writeoff'] == report['result']['writeoff']
    assert json.loads(json.dumps(exported)) == exported


def test_segment_evidence_states_writeoff_slice(client):
    from app.samples import load_sample
    from app.times import instant

    data = load_sample('dirty')
    report = client.post('/api/runs', json={'data': data}).json()
    covered = client.get(f"/api/runs/{report['id']}/evidence",
                         params={'device': '001', 'start': instant('2026-11-19T22:30:00+08:00')}).json()
    assert {entry['id']: entry['slice'] for entry in covered['writeoff']} == {
        'P-101': 'written', 'P-102': 'written', 'P-103': 'unwritten'}
    assert next(entry for entry in covered['writeoff'] if entry['id'] == 'P-103')['code'] == 'HIGHER_PRIORITY'

    conflict = client.get(f"/api/runs/{report['id']}/evidence",
                          params={'device': '002', 'start': instant('2026-11-20T00:10:00+08:00')}).json()
    assert conflict['segment']['bucket'] == 'missing' and conflict['writeoff'] == []
