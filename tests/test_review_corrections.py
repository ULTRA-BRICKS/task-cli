"""Review regressions through real adapters and CLI read boundaries.

Only transport/provider construction is substituted; no production services are used.
"""
import http.client
import json
import urllib.error

import pytest

from tasklib import cli
from tasklib.backends import AmbiguousBackendError, BackendError
from tasklib.backends import http as transport
from tasklib.backends.linear import LinearBackend
from tasklib.backends.tasks import TasksBackend
from tasklib.model import Ticket
from tasklib.projects import projects_from_config

TOKEN = 'private-synthetic-token'
ROW = {'id': 'issue', 'key': 'HYP-1', 'title': 'Synthetic', 'description': 'private document'}


def tasks_backend():
    backend = TasksBackend(url='https://tasks.example.test', token=TOKEN, team_key='HYP')
    backend._team_id = 'team'
    backend._statuses = [{'id': 'todo', 'category': 'unstarted', 'name': 'Todo', 'team_id': 'team'}]
    return backend


@pytest.mark.parametrize('helper', [transport.request_json, transport.request_bytes])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('phase', ['open', 'body'])
def test_safe_http_failure_is_clean(monkeypatch, helper, method, phase):
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            raise http.client.IncompleteRead(b'private payload')
    def opener(*args, **kwargs):
        if phase == 'open':
            raise urllib.error.URLError(TimeoutError(TOKEN))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen', opener)
    with pytest.raises(transport.HttpError) as caught:
        helper('https://example.test/issues', method=method)
    assert 'may have' not in str(caught.value)
    assert TOKEN not in str(caught.value)
    assert 'private payload' not in str(caught.value)


@pytest.mark.parametrize('operation', ['list', 'search'])
@pytest.mark.parametrize('failure_at', ['query', 'resolution'])
def test_aggregation_keeps_healthy_project_after_graphql_failure(monkeypatch, operation, failure_at):
    projects = projects_from_config({'projects': [{'team': 'BAD'}, {'tasks': {'url': 'https://tasks.example.test', 'team': 'HYP'}}]})
    linear = LinearBackend(api_key=TOKEN, team_key='BAD')
    healthy = tasks_backend()
    def factory(cfg):
        if cfg['backend'] == 'linear':
            if failure_at == 'resolution':
                raise AmbiguousBackendError('response lost')
            return linear
        return healthy
    monkeypatch.setattr(cli, '_config_for_overlay', lambda cfg, overlay: overlay)
    monkeypatch.setattr('tasklib.backends.get_backend', factory)
    def fail_open(*args, **kwargs):
        raise TimeoutError(TOKEN)
    monkeypatch.setattr('urllib.request.urlopen', fail_open)
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', lambda *args, **kwargs: json.dumps({'data': [ROW]}).encode())
    if operation == 'list':
        groups = cli._aggregate_projects(None, projects, labels=None, state=None, limit=30)
    else:
        groups = cli._search_projects(None, projects, 'Synthetic', state=None, limit=30)
    assert len(groups) == 2
    assert groups[0].error
    assert 'may have' not in groups[0].error
    assert [t.id for t in groups[1].tickets] == ['HYP-1']
    assert groups[1].error is None


@pytest.mark.parametrize('command', [['read', 'BAD-1'], ['find', 'Synthetic'], ['status', 'BAD-1']])
@pytest.mark.parametrize('failure_at', ['query', 'resolution'])
def test_direct_readonly_graphql_errors_are_clean(monkeypatch, capsys, command, failure_at):
    backend = LinearBackend(api_key=TOKEN, team_key='BAD')
    monkeypatch.setattr(cli, '_load', lambda args: None)
    monkeypatch.setattr(cli, '_current_project_overlay', lambda cfg: {})
    def factory(cfg):
        if failure_at == 'resolution':
            raise AmbiguousBackendError('response lost')
        return backend
    monkeypatch.setattr('tasklib.backends.get_backend', factory)
    def fail_open(*args, **kwargs):
        raise TimeoutError(TOKEN)
    monkeypatch.setattr('urllib.request.urlopen', fail_open)
    assert cli.main(command) == 2
    output = capsys.readouterr().out
    assert 'error:' in output
    assert 'may have' not in output
    assert TOKEN not in output


@pytest.mark.parametrize('bad', [None, 'private payload', {}, {'body': None}, {'body': 7}, {'body': ['private payload']}])
@pytest.mark.parametrize('machine', [False, True])
def test_malformed_comment_fails_before_human_or_json_output(monkeypatch, capsys, bad, machine):
    backend = tasks_backend()
    monkeypatch.setattr(cli, '_load', lambda args: None)
    monkeypatch.setattr(cli, '_current_project_overlay', lambda cfg: {})
    monkeypatch.setattr('tasklib.backends.get_backend', lambda cfg: backend)
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', lambda *args, **kwargs: json.dumps({**ROW, 'comments': [{'body': 'valid'}, bad]}).encode())
    marks = []
    monkeypatch.setattr(cli, '_mark_ticket_read', lambda *args: marks.append(args))
    assert cli.main(['read', 'HYP-1'] + (['--json'] if machine else [])) == 2
    output = capsys.readouterr().out
    assert 'error:' in output and 'comment' in output
    assert 'private' not in output
    assert 'Comments (fetched' not in output
    assert marks == []


@pytest.mark.parametrize('comments', [None, [], [{'body': ''}], [{'body': 'a', 'author_name': None}, {'body': 'b', 'source': {'id': 'original'}}]])
def test_optional_comment_fields_are_preserved(monkeypatch, comments):
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', lambda *args, **kwargs: json.dumps({**ROW, 'comments': comments}).encode())
    assert tasks_backend().get('HYP-1').comments == comments


@pytest.mark.parametrize('raw', [b'<html>private payload</html>', b'\xffprivate payload'])
def test_tasks_malformed_get_is_clean_and_informative(monkeypatch, raw):
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return raw
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    with pytest.raises(BackendError, match='malformed.*GET') as caught:
        tasks_backend().get('HYP-1')
    assert 'private payload' not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize('raw', [b'not JSON', b'\xff', b'{}', b''])
@pytest.mark.parametrize('recovers', [False, True])
def test_unusable_create_receipt_reuses_key_or_stays_ambiguous(monkeypatch, raw, recovers):
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return json.dumps(ROW).encode() if recovers and len(calls) == 2 else raw
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    if recovers:
        assert tasks_backend().create(Ticket(title='Synthetic')).id == 'HYP-1'
    else:
        with pytest.raises(AmbiguousBackendError, match='may have been created'):
            tasks_backend().create(Ticket(title='Synthetic'))
    assert len(calls) == 2
    assert calls[0]['headers']['Idempotency-Key'] == calls[1]['headers']['Idempotency-Key']
    assert calls[0]['data'] == calls[1]['data']


@pytest.mark.parametrize('method', ['POST', 'PATCH', 'PUT', 'DELETE'])
def test_nonkeyed_mutation_bad_json_remains_ambiguous_without_retry(monkeypatch, method):
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return b'not JSON'
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    with pytest.raises(AmbiguousBackendError):
        tasks_backend()._call('/api/v1/issues/issue', method=method, payload={'title': 'Synthetic'})
    assert len(calls) == 1


@pytest.mark.parametrize('status', [409, 422, 500])
def test_recovery_rejection_keeps_scrubbed_detail_and_original_uncertainty(monkeypatch, status):
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise transport.AmbiguousHttpError('lost response')
        raise transport.HttpError(status, f'HTTP {status}', f'key conflict; Authorization: Bearer {TOKEN}')
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    with pytest.raises(AmbiguousBackendError) as caught:
        tasks_backend().create(Ticket(title='Synthetic'))
    message = str(caught.value)
    assert str(status) in message and 'key conflict' in message
    assert TOKEN not in message
    assert 'may have been created' in message
    assert len(calls) == 2
    assert calls[0]['data'] == calls[1]['data']
    assert calls[0]['headers']['Idempotency-Key'] == calls[1]['headers']['Idempotency-Key']
