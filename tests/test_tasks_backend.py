"""Hermetic native Tasks adapter coverage; no real credentials or live requests."""
import json
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qs

import pytest

from tasklib.backends import BackendError, TicketBackend, get_backend, backend_coordinate
from tasklib.backends.tasks import TasksBackend
from tasklib.model import State

ORIGIN = 'https://tasks.example.test'
TOKEN = 'synthetic-test-token'
ROW = {'id':'11111111-1111-4111-8111-111111111111','key':'HYP-1','title':'Original',
       'description':'Original unstructured Markdown.','version':7,'team_id':'team-1',
       'status_id':'todo','status_category':'unstarted','status_name':'Todo','labels':[],
       'created_at':'2026-01-01T00:00:00Z','updated_at':'2026-01-02T00:00:00Z'}

@pytest.fixture
def api(monkeypatch):
    calls=[]
    def request(url, *, method='GET', headers=None, data=None, **options):
        assert url.startswith(ORIGIN+'/api/v1/')
        assert headers['Authorization']=='Bearer '+TOKEN
        assert options['same_host_redirects_only'] is True
        payload=json.loads(data) if data and headers.get('Content-Type')=='application/json' else data
        calls.append((method,url,payload,headers))
        path=urlsplit(url).path
        if path.endswith('/catalog'):
            result={'teams':[{'id':'team-1','key':'HYP'}],'statuses':[
                {'id':'todo','team_id':'team-1','category':'unstarted','name':'Todo'},
                {'id':'doing','team_id':'team-1','category':'started','name':'In progress'},
                {'id':'done','team_id':'team-1','category':'completed','name':'Done'}], 'labels':[]}
        elif path.endswith('/me'): result={'id':'viewer'}
        elif path=='/api/v1/issues':
            cursor=parse_qs(urlsplit(url).query).get('cursor')
            result={'data':[{**ROW,'title':'Second' if cursor else 'Original','key':'HYP-2' if cursor else 'HYP-1'}], 'next_cursor':None if cursor else 'page-2'}
        elif method=='PATCH': result={**ROW,**payload,'version':8}
        else: result=ROW
        return json.dumps(result).encode()
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes',request)
    return calls

def backend(): return TasksBackend(url=ORIGIN,token=TOKEN,team_key='HYP')

def test_protocol_get_and_credential_safe_representation(api):
    b=backend(); assert isinstance(b,TicketBackend)
    t=b.get('HYP-1')
    assert t.id=='HYP-1' and t.raw_body==ROW['description']
    assert t.backend_version==7
    assert t.url==ORIGIN+'/issues/'+ROW['id']
    assert TOKEN not in repr(b)
    assert b.current_user()=='viewer'

def test_pagination_and_title_body_search(api):
    b=backend(); assert len(b.list(limit=2))==2
    assert len(b.search('unstructured',limit=2))==2
    assert len([x for x in api if urlsplit(x[1]).path=='/api/v1/issues'])==4

def test_transition_preserves_original_description_and_uses_read_version(api):
    t=backend().transition('HYP-1',State.DONE)
    write=next(x for x in api if x[0]=='PATCH')
    assert write[2]['version']==7 and write[2]['status_id']=='done'
    assert 'description' not in write[2]
    assert t.backend_version==8

def test_no_silent_in_review_fallback(api):
    with pytest.raises(BackendError,match='review'):
        backend().transition('HYP-1',State.IN_REVIEW)
    assert not any(x[0]=='PATCH' for x in api)

def test_tasks_config_factory_and_distinct_coordinate(api):
    c=SimpleNamespace(backend='tasks',section=lambda name:{'url':ORIGIN,'team':'HYP'})
    b=get_backend(c,env={'TASKS_TOKEN':TOKEN,'TASKS_URL':ORIGIN})
    assert b.name=='tasks'
    assert backend_coordinate(b).startswith('tasks:'+ORIGIN)

@pytest.mark.parametrize('url',['http://remote.test','https://user:pass@example.test','https://example.test/?token=bad','https://example.test/path','file:///tmp/test'])
def test_unsafe_origins_rejected(url):
    with pytest.raises(BackendError): TasksBackend(url=url,token=TOKEN,team_key='HYP')

def test_untrusted_attachment_does_not_receive_token(api):
    with pytest.raises(BackendError): backend().fetch_attachment_bytes('https://elsewhere.test/file.png')
    assert api==[]


def test_env_token_is_bound_to_its_paired_url():
    from tasklib.credentials import CredentialError, tasks_token
    with pytest.raises(CredentialError):
        tasks_token(ORIGIN, {'HOME': '/nonexistent', 'TASKS_TOKEN': TOKEN})
    with pytest.raises(CredentialError):
        tasks_token(ORIGIN, {'HOME': '/nonexistent', 'TASKS_TOKEN': TOKEN, 'TASKS_URL': 'https://attacker.example'})
    assert tasks_token(ORIGIN, {'HOME': '/nonexistent', 'TICKETS_TOKEN': TOKEN, 'TICKETS_URL': ORIGIN+'/'}).token == TOKEN

def test_registry_and_live_tasks_backend_share_one_coordinate():
    from tasklib.projects import projects_from_config
    (p,) = projects_from_config({'projects': [{'tasks': {'url': 'https://Tasks.Example.test:443/', 'team': 'hyp'}}]})
    assert p.coordinate == backend_coordinate(backend())


def test_tasks_errors_never_echo_the_token_or_url_credentials(monkeypatch):
    from tasklib.backends.http import HttpError
    def reflect(url, **kw):
        raise HttpError(500, 'HTTP 500', 'debug: Authorization: Bearer ' + TOKEN)
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', reflect)
    with pytest.raises(BackendError) as info:
        backend().get('HYP-1')
    assert TOKEN not in str(info.value)
    for url in ('https://admin:hunter2-secret@tasks.example.test', 'https://tasks.example.test/?token=hunter2-secret'):
        with pytest.raises(BackendError) as info:
            TasksBackend(url=url, token=TOKEN, team_key='HYP')
        assert 'hunter2-secret' not in str(info.value)


@pytest.mark.parametrize("operation", ["read", "comment", "transition"])
def test_foreign_team_key_is_rejected_before_any_request(api, operation):
    instance = backend()
    with pytest.raises(BackendError, match="configured.*HYP"):
        if operation == "read":
            instance.get("GL-264")
        elif operation == "comment":
            instance.comment("GL-264", "Must not be written")
        else:
            instance.transition("GL-264", State.DONE)
    assert api == []


@pytest.mark.parametrize("code", ["not-a-ticket", "https://linear.app/glide/issue/GL-264", "GL-264/extra"])
def test_malformed_or_other_tracker_reference_is_not_a_tasks_lookup(api, code):
    with pytest.raises(BackendError, match="Tasks identifier"):
        backend().get(code)
    assert api == []


def test_explicit_uuid_remains_a_supported_tasks_lookup(api):
    ticket = backend().get(ROW["id"])
    assert ticket.url.endswith(ROW["id"])
    assert len(api) == 1


def test_cross_team_update_refuses_before_creating_labels(api):
    from dataclasses import replace
    b = backend()
    ticket = b.get('HYP-1')
    api.clear()
    with pytest.raises(BackendError, match='configured.*HYP'):
        b.update(replace(ticket, id='GL-264', labels=['new-label']))
    assert api == []


def test_comment_refuses_a_response_for_a_different_identifier(monkeypatch):
    b = backend()
    calls = []
    def wrong_issue(path, **kwargs):
        calls.append((path, kwargs))
        return {**ROW, 'key': 'HYP-2'}
    monkeypatch.setattr(b, '_call', wrong_issue)
    with pytest.raises(BackendError, match='different issue'):
        b.comment('HYP-1', 'Must not attach to HYP-2')
    assert len(calls) == 1
    assert calls[0][1].get('method', 'GET') == 'GET'


def test_uuid_lookup_refuses_a_response_for_a_different_uuid(monkeypatch):
    b = backend()
    monkeypatch.setattr(b, '_call', lambda *args, **kwargs: {**ROW, 'id': '22222222-2222-4222-8222-222222222222'})
    with pytest.raises(BackendError, match='different issue'):
        b.get(ROW['id'])



def test_comment_uses_verified_uuid_not_a_reassignable_short_key(api):
    backend().comment('hyp-1', 'Verified target')
    assert api[-1][1].endswith('/issues/' + ROW['id'] + '/comments')
    assert api[-1][0] == 'POST'


def test_foreign_update_on_a_cold_backend_makes_no_requests(api):
    from dataclasses import replace
    b = backend()
    ticket = b._row_to_ticket(ROW)
    assert b._team_id == ''
    with pytest.raises(BackendError, match='configured.*HYP'):
        b.update(replace(ticket, id='GL-264', labels=['new-label']))
    assert api == []


def test_invalid_provider_url_is_not_reflected_in_errors(api):
    url = 'https://linear.app/glide/issue/GL-264?access=fixture-private-value'
    with pytest.raises(BackendError) as error:
        backend().get(url)
    assert 'fixture-private-value' not in str(error.value)
    assert api == []



def test_invalid_reference_is_not_leaked_by_exception_traceback(api):
    import traceback
    url = 'https://linear.app/glide/issue/GL-264?access=fixture-private-value'
    with pytest.raises(BackendError) as error:
        backend().get(url)
    rendered = ''.join(traceback.format_exception(error.value))
    assert 'fixture-private-value' not in rendered
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize('form', [ROW['id'].replace('-', ''), '{' + ROW['id'] + '}', 'urn:uuid:' + ROW['id']])
def test_uuid_forms_outside_the_canonical_contract_are_refused(api, form):
    with pytest.raises(BackendError, match='Tasks identifier'):
        backend().get(form)
    assert api == []


def test_uppercase_uuid_request_is_canonicalized(monkeypatch):
    uid = 'abcdefab-1234-4567-8abc-123456abcdef'
    b = backend()
    paths = []
    def request(path, **kwargs):
        paths.append(path)
        return {**ROW, 'id': uid}
    monkeypatch.setattr(b, '_call', request)
    b.get(uid.upper())
    assert paths == ['/api/v1/issues/' + uid]


def test_uuid_does_not_apply_configured_team_status_to_a_foreign_team(api, monkeypatch):
    b = backend()
    b._ensure_catalog()
    api.clear()
    calls = []
    def request(path, **kwargs):
        calls.append((path, kwargs))
        return {**ROW, 'key': 'GL-264', 'team_id': 'team-gl'}
    monkeypatch.setattr(b, '_call', request)
    with pytest.raises(BackendError, match='configured.*HYP'):
        b.transition(ROW['id'], State.DONE)
    assert len(calls) == 1
    assert calls[0][1].get('method', 'GET') == 'GET'



def test_update_requires_the_team_key_returned_by_the_original_read(api):
    from dataclasses import replace
    b = backend()
    ticket = b.get(ROW['id'])
    assert ticket.id == 'HYP-1'
    api.clear()
    with pytest.raises(BackendError, match='configured.*HYP'):
        b.update(replace(ticket, id=ROW['id'], labels=['new-label']))
    assert api == []


def test_uuid_read_then_update_preserves_normal_supported_flow(api):
    b = backend()
    ticket = b.get(ROW['id'])
    result = b.update(ticket)
    assert result.id == 'HYP-1'
    write = next(call for call in api if call[0] == 'PATCH')
    assert write[2]['version'] == ROW['version']
