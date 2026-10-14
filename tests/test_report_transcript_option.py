"""Exercise the shipped report dialog and payload without filing real reports."""
import json

import pytest

from tests.test_voice_speech_text import _js_function, _run, pytestmark  # noqa: F401


def _dialog(actions, messages=None, public=None):
    turns = messages if messages is not None else [
        {"role": "user", "content": "private question"},
        {"role": "assistant", "content": "private answer"},
    ]
    functions = '\n'.join(_js_function(name) for name in (
        'updateReportContext', 'openReportModal', 'closeReportModal', 'submitReport',
    ))
    harness = '''
const elements = new Map();
const document = { getElementById(id) {
  if (!elements.has(id)) elements.set(id, {
    value: 'Something went wrong', textContent: '', innerHTML: '', checked: false,
    disabled: false, style: {}, classList: {add(){},remove(){}}, focus(){},
  });
  return elements.get(id);
}};
const API = '/api';
let _reportTarget = null, _chatConvId = 'conv-private';
const _lastRouting = {routed: 'test-model'};
let payload, receipt;
const target = {
  querySelector: () => ({textContent: 'private answer'}),
  insertAdjacentHTML: (_, html) => {receipt = html;},
};
const button = {closest: () => target};
async function fetch(url, options) {
  payload = JSON.parse(options.body);
  return {ok: true, json: async () => ({id: 7, issue_url: ''})};
}
'''
    harness += 'let _chatMessages = ' + json.dumps(turns) + ';\n'
    harness += 'async function fetchWhoAmI() { return ' + json.dumps(public or {}) + '; }\n'
    harness += functions + '\n(async () => {\n' + actions + '\n})().catch(e => {throw e;});'
    return _run(harness)


@pytest.mark.parametrize('from_bubble', [True, False])
@pytest.mark.parametrize('include', [True, False])
def test_report_payload_respects_transcript_choice(from_bubble, include):
    payload, receipt = _dialog(f'''
openReportModal({'button' if from_bubble else 'null'});
document.getElementById('report-modal-text').value = 'Bad answer';
document.getElementById('report-include-transcript').checked = {json.dumps(include)};
await submitReport();
console.log(JSON.stringify([payload, receipt || '']));
''')
    assert payload['meta']['routed'] == 'test-model'
    if include:
        assert len(payload['transcript']) == 2
        assert payload['conversation_id'] == 'conv-private'
        if from_bubble:
            assert payload['meta']['reported_text'] == 'private answer'
            assert 'attached thread' in receipt
    else:
        assert payload['transcript'] == []
        assert payload['conversation_id'] == ''
        assert 'reported_text' not in payload['meta']
        assert 'private' not in json.dumps(payload)
        assert 'thread' not in receipt


def test_transcript_is_opt_in_each_time_dialog_opens():
    checked, disabled, note = _dialog('''
openReportModal(button);
document.getElementById('report-include-transcript').checked = true;
closeReportModal();
openReportModal(null);
await Promise.resolve();
console.log(JSON.stringify([
  document.getElementById('report-include-transcript').checked,
  document.getElementById('report-include-transcript').disabled,
  document.getElementById('report-modal-note').textContent,
]));
''')
    assert checked is False
    assert disabled is False
    assert 'no chat content' in note


def test_no_open_chat_disables_transcript_option():
    disabled, context = _dialog('''
openReportModal(null);
console.log(JSON.stringify([
  document.getElementById('report-include-transcript').disabled,
  document.getElementById('report-modal-context').textContent,
]));
''', messages=[])
    assert disabled is True
    assert context == 'No chat open in this tab.'


@pytest.mark.parametrize('publish_transcript', [True, False])
def test_public_destination_note_follows_current_choice(publish_transcript):
    included, excluded = _dialog('''
openReportModal(button);
document.getElementById('report-include-transcript').checked = true;
updateReportContext();
await Promise.resolve();
const included = document.getElementById('report-modal-note').textContent;
document.getElementById('report-include-transcript').checked = false;
updateReportContext();
await Promise.resolve();
console.log(JSON.stringify([included, document.getElementById('report-modal-note').textContent]));
''', public={'reports_public': True, 'reports_public_transcript': publish_transcript})
    assert 'public GitHub issue' in included
    if publish_transcript:
        assert 'including the transcript' in included
    else:
        assert 'transcript stays on this router' in included
    assert 'no chat content is attached' in excluded
    assert 'including the transcript' not in excluded
