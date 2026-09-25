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
