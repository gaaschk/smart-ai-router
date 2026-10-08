from smart_ai_router.models import UsageRecord
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.facade import CapabilityRouter
from tests.test_settings import _client, _ADMIN


def test_media_totals_scoped_and_historical_rows_unclassified(monkeypatch):
    monkeypatch.setenv('SMART_ROUTER_API_KEYS', _ADMIN)
    store = SqliteStore(':memory:')
    cr = CapabilityRouter(store=store)
    for modality, domain, cost in [('text', '', .1), ('image', '', .2),
                                    ('sound', 'voice', .3), ('', '', .4), ('', 'voice', .5)]:
        cr.record_usage(UsageRecord(user='admin', modality=modality, domain=domain,
            prompt_tokens=10, completion_tokens=20, cost_usd=cost))
    cr.record_usage(UsageRecord(user='alice', modality='image', cost_usd=99))
    cr.record_usage(UsageRecord(user='admin', modality='text', kind='classify', cost_usd=88))
    client = _client(cr)
    data = client.get('/api/overview', headers={'Authorization': f'Bearer {_ADMIN}'}).json()
    rows = {r['key']: r for r in data['modalities']}
    assert rows['text']['cost_usd'] == .1
    assert rows['image']['cost_usd'] == .2
    assert rows['sound']['cost_usd'] == .8
    assert rows['sound']['requests'] == 2
    assert rows['unclassified']['cost_usd'] == .4
    assert sum(r['requests'] for r in rows.values()) == 5
    assert {r.modality for r in store.recent_usage('admin', '')} == {'', 'text', 'sound', 'image'}


def test_media_ui_does_not_invent_historical_counts():
    from tests.test_voice_speech_text import _js_function, _run
    source = _js_function('renderMediaUsage')
    out = _run(source + '''
const result = {};
const _setText = (id, text) => result[id] = text;
const _fmtCost = n => '$' + n.toFixed(2);
const _fmtTokens = n => String(n);
renderMediaUsage({modalities: [
 {key:'text', requests:2, cost_usd:.1, prompt_tokens:10, completion_tokens:20},
 {key:'unclassified', requests:5, cost_usd:.4}
]});
console.log(JSON.stringify(result));
''')
    assert '10 input / 20 output' in out['media-text']
    assert 'No classified requests' in out['media-image']
    assert 'incomplete' in out['media-sound']
    assert '5 older or unclassified' in out['media-unclassified']
