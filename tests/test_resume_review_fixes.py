"""Adapter-boundary regressions for read uncertainty and confirmed write receipts.

Real adapters/CLI run against synthetic transports; receipt tests retain session evidence.
"""
import json
import urllib.error

import pytest

from tasklib import cli, read_tracker, session
from tasklib.backends import AmbiguousBackendError, BackendError, backend_coordinate
from tasklib.backends.linear import LinearBackend
from tasklib.model import State, Ticket
from tests.test_review_corrections import TOKEN, tasks_backend


@pytest.fixture(params=[TimeoutError, lambda text: urllib.error.URLError(TimeoutError(text))])
def failed_network(request, monkeypatch):
    calls = []

    def opener(req, **kwargs):
        calls.append(json.loads(req.data))
        raise request.param(TOKEN)

    monkeypatch.setattr('urllib.request.urlopen', opener)
    return calls


@pytest.mark.parametrize('command', [
    ['change', 'HYP-1', '--title', 'Changed'],
    ['status', 'HYP-1', 'in-progress'],
    ['done', 'HYP-1'],
])
@pytest.mark.parametrize('machine', [False, True])
def test_linear_initial_reads_fail_cleanly_without_write(monkeypatch, capsys, failed_network, command, machine):
    backend = LinearBackend(api_key=TOKEN, team_key='HYP')
    monkeypatch.setattr(cli, '_load', lambda args: None)
    monkeypatch.setattr(cli, '_backend_for_id', lambda cfg, ident: backend)
    assert cli.main(command + (['--json'] if machine else [])) == 2
    output = capsys.readouterr().out
    assert 'error:' in output and 'may have' not in output and TOKEN not in output
    assert len(failed_network) == 1
    assert failed_network[0]['query'].startswith('query')


@pytest.mark.parametrize('operation', [
    'get', 'list', 'search', 'create', 'update', 'transition', 'comment', 'attach',
    'state_catalog', 'label_catalog',
])
def test_linear_read_and_prewrite_queries_are_clean(failed_network, operation):
    backend = LinearBackend(api_key=TOKEN, team_key='HYP')
    ticket = Ticket(id='HYP-1', title='Synthetic')
    operations = {
        'get': lambda: backend.get(ticket.id),
        'list': backend.list,
        'search': lambda: backend.search('Synthetic'),
        'create': lambda: backend.create(ticket),
        'update': lambda: backend.update(ticket),
        'transition': lambda: backend.transition(ticket.id, State.IN_PROGRESS),
        'comment': lambda: backend.comment(ticket.id, 'Synthetic'),
        'attach': lambda: backend.attach(ticket.id, 'https://example.test/image.png'),
        'state_catalog': lambda: backend._state_id_for(State.TODO),
        'label_catalog': lambda: backend._label_ids(['synthetic']),
    }
    with pytest.raises(BackendError) as caught:
        operations[operation]()
    assert 'may have' not in str(caught.value) and TOKEN not in str(caught.value)
    assert len(failed_network) == 1
    assert failed_network[0]['query'].startswith('query')


def test_linear_viewer_is_best_effort(failed_network):
    assert LinearBackend(api_key=TOKEN, team_key='HYP').current_user() is None
    assert len(failed_network) == 1


@pytest.mark.parametrize('operation', ['create', 'label', 'opaque'])
def test_linear_writes_and_unknown_intent_stay_ambiguous(failed_network, operation):
    backend = LinearBackend(api_key=TOKEN, team_key='HYP')
    backend._team_id = 'team'
    backend._states_by_type = {'unstarted': 'todo'}
    with pytest.raises(AmbiguousBackendError):
        if operation == 'create':
            backend.create(Ticket(title='Synthetic'))
        elif operation == 'label':
            backend._label_ids(['missing'])
        else:
            backend._gql('opaque operation')
    assert len(failed_network) == 1


@pytest.mark.parametrize('operation', ['create', 'update', 'transition'])
def test_tasks_receipts_do_not_claim_discussion_complete(monkeypatch, operation):
    backend = tasks_backend()
    row = {'id': 'receipt', 'key': 'HYP-1', 'title': 'Synthetic', 'version': 1,
           'description': '', 'comments': [{'body': None}]}
    calls = []

    def request(url, **kwargs):
        calls.append(kwargs['method'])
        return json.dumps(row).encode()

    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    if operation == 'create':
        ticket = backend.create(Ticket(title='Synthetic'))
    elif operation == 'update':
        ticket = backend.update(Ticket(id='HYP-1', title='Synthetic', backend_version=1))
    else:
        ticket = backend.transition('HYP-1', State.TODO)
    assert ticket.id == 'HYP-1'
    assert ticket.comments is None
    assert calls == (['POST'] if operation == 'create' else ['PATCH'] if operation == 'update' else ['GET', 'PATCH'])
    with pytest.raises(BackendError, match='comments'):
        backend.get('HYP-1')


@pytest.mark.parametrize('operation', ['create', 'update'])
@pytest.mark.parametrize('machine', [False, True])
def test_tasks_confirmed_receipts_keep_cli_success_and_bookkeeping(monkeypatch, isolated_state, capsys, operation, machine):
    backend = tasks_backend()
    monkeypatch.setenv('TASK_SESSION', 'receipt-session')
    monkeypatch.setattr('tasklib.backends.get_backend', lambda cfg, env=None: backend)
    backend._label_by_name = {'session:receipt-session': 'session-label'}
    calls = []

    def request(url, **kwargs):
        method = kwargs['method']
        calls.append(method)
        if '?' in url:
            return b'{"data": []}'
        row = {'id': 'receipt', 'key': 'HYP-1', 'title': 'Synthetic', 'version': 1,
               'description': '', 'labels': ['session:receipt-session']}
        if method != 'GET':
            row['comments'] = [{'body': None}]
        return json.dumps(row).encode()

    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    if operation == 'create':
        command = ['create', '--title', 'Synthetic receipt', '--what', 'Keep confirmed receipts',
                   '--why', 'Users need the receipt', '--impact',
                   'Users can see whether their ticket was created after a slow connection, so they can continue their work without accidentally submitting the same request twice.',
                   '--if-not-done', 'Users may create duplicate tickets.',
                   '--acceptance', 'Return the saved receipt', '--acceptance', 'Keep uncertain outcomes explicit']
    else:
        command = ['change', 'HYP-1', '--title', 'Synthetic changed']
    assert cli.main(command + (['--json'] if machine else [])) == 0
    output = capsys.readouterr().out
    assert (json.loads(output)['id'] == 'HYP-1') if machine else ('HYP-1' in output)
    assert calls.count('POST') == (1 if operation == 'create' else 0)
    assert calls.count('PATCH') == (1 if operation == 'update' else 0)
    assert session.read_ids('receipt-session') == ['HYP-1']
    assert read_tracker.is_fresh('receipt-session', 'HYP-1', repo=backend_coordinate(backend)) is (operation == 'create')


def test_linear_cached_catalog_does_not_hide_list_failure(failed_network):
    backend = LinearBackend(api_key=TOKEN, team_key='HYP')
    backend._team_id = 'team'
    with pytest.raises(BackendError):
        backend.list()
    assert len(failed_network) == 1
    assert 'issues(filter:' in failed_network[0]['query']


@pytest.mark.parametrize('failure_at', ['catalog', 'mutation'])
def test_linear_update_catalog_failure_is_clean_but_write_failure_is_not(monkeypatch, failure_at):
    backend = LinearBackend(api_key=TOKEN, team_key='HYP')
    calls = []

    class Response:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({'data': self.data}).encode()

    def opener(req, **kwargs):
        query = json.loads(req.data)['query']
        calls.append(query)
        if len(calls) == 1:
            return Response({'issue': {'id': 'internal', 'identifier': 'HYP-1'}})
        if len(calls) == 2 and failure_at == 'mutation':
            return Response({'teams': {'nodes': [{'id': 'team', 'states': {'nodes': [
                {'id': 'todo', 'type': 'unstarted'}]}}]}})
        raise TimeoutError(TOKEN)

    monkeypatch.setattr('urllib.request.urlopen', opener)
    with pytest.raises(BackendError if failure_at == 'catalog' else AmbiguousBackendError):
        backend.update(Ticket(id='HYP-1', title='Synthetic'))
    assert len(calls) == (2 if failure_at == 'catalog' else 3)
    assert all(query.startswith('query') for query in calls[:2])
    if failure_at == 'mutation':
        assert calls[-1].startswith('mutation')
