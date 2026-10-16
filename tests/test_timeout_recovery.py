"""Transport uncertainty and bounded tasks create recovery with synthetic payloads.

The loopback server tests the committed-write/lost-response window without production I/O.
"""
import http.client
import json
import urllib.error

import pytest

from tasklib.backends import AmbiguousBackendError, BackendError
from tasklib.backends import http as transport
from tasklib.backends.tasks import TasksBackend
from tasklib.model import Ticket


@pytest.mark.parametrize('helper', [transport.request_bytes, transport.request_json])
@pytest.mark.parametrize('error', [TimeoutError('synthetic secret'), urllib.error.URLError(TimeoutError('synthetic secret')), http.client.RemoteDisconnected('synthetic secret'), http.client.BadStatusLine('synthetic secret')])
def test_header_failures_are_ambiguous(monkeypatch, helper, error):
    def opener(*args, **kwargs):
        raise error
    monkeypatch.setattr('urllib.request.urlopen', opener)
    with pytest.raises(transport.AmbiguousHttpError) as caught:
        helper('https://example.test/issues', method='POST')
    assert 'synthetic secret' not in str(caught.value)
    assert 'may have completed' in str(caught.value)


@pytest.mark.parametrize('helper', [transport.request_bytes, transport.request_json])
@pytest.mark.parametrize('error', [urllib.error.URLError(ConnectionRefusedError('refused')), ConnectionRefusedError('refused'), http.client.InvalidURL('invalid')])
def test_proven_pre_send_errors_are_clean(monkeypatch, helper, error):
    def opener(*args, **kwargs):
        raise error
    monkeypatch.setattr('urllib.request.urlopen', opener)
    with pytest.raises(transport.HttpError):
        helper('https://example.test/issues', method='POST')


def configured_backend():
    backend = TasksBackend(url='https://tasks.example.test', token='private-synthetic-token', team_key='HYP')
    backend._team_id = 'team'
    backend._statuses = [{'id': 'todo', 'category': 'unstarted', 'name': 'Todo', 'team_id': 'team'}]
    return backend


@pytest.mark.parametrize('first_error', [transport.AmbiguousHttpError('response lost'), transport.HttpError(422, 'invalid')])
def test_create_recovery_only_for_ambiguity_same_bytes_and_key(monkeypatch, first_error):
    calls = []
    def request(url, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise first_error
        return json.dumps({'id': 'issue', 'key': 'HYP-1', 'description': 'saved'}).encode()
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    backend = configured_backend()
    if isinstance(first_error, transport.HttpError):
        with pytest.raises(BackendError):
            backend.create(Ticket(title='Synthetic'))
        assert len(calls) == 1
    else:
        assert backend.create(Ticket(title='Synthetic')).id == 'HYP-1'
        assert len(calls) == 2
        assert calls[0]['headers']['Idempotency-Key'] == calls[1]['headers']['Idempotency-Key']
        assert calls[0]['data'] == calls[1]['data']


def test_unrecoverable_create_is_bounded_and_explicit(monkeypatch):
    calls = []
    def request(url, **kwargs):
        calls.append(kwargs)
        raise transport.AmbiguousHttpError('lost response')
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    with pytest.raises(AmbiguousBackendError, match='may have'):
        configured_backend().create(Ticket(title='Synthetic'))
    assert len(calls) == 2


def test_other_mutations_are_not_replayed(monkeypatch):
    calls = []
    def request(url, **kwargs):
        calls.append(kwargs)
        raise transport.AmbiguousHttpError('lost response')
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    with pytest.raises(AmbiguousBackendError):
        configured_backend()._call('/api/v1/issues/issue', method='PATCH', payload={'title': 'new'})
    assert len(calls) == 1


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_committed_create_with_delayed_http_response_recovers_once(monkeypatch, phase):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    operations = {}
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            key = self.headers.get('Idempotency-Key')
            payload = self.rfile.read(int(self.headers['Content-Length']))
            requests.append((key, payload))
            if key in operations:
                assert operations[key][0] == payload
            else:
                operations[key] = (payload, {'id': 'receipt', 'key': 'HYP-42', 'description': 'saved'})
            response = json.dumps(operations[key][1]).encode()
            first = len(requests) == 1
            if first and phase == 'headers':
                time.sleep(0.15)
            self.send_response(201)
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            if first and phase == 'body':
                time.sleep(0.15)
            try:
                self.wfile.write(response)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    worker.start()
    real_request = transport.request_bytes
    def local_request(url, **kwargs):
        return real_request(f'http://127.0.0.1:{server.server_port}/api/v1/issues', timeout=0.05, **kwargs)
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', local_request)
    try:
        ticket = configured_backend().create(Ticket(title='Delayed synthetic create'))
        assert ticket.id == 'HYP-42'
        assert len(operations) == 1
        assert len(requests) == 2
        assert requests[0] == requests[1]
        assert requests[0][0].startswith('task-create:')
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


def test_separate_create_operations_get_distinct_keys(monkeypatch):
    keys = []
    def request(url, **kwargs):
        keys.append(kwargs['headers']['Idempotency-Key'])
        return b'{"id":"receipt","key":"HYP-1"}'
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    backend = configured_backend()
    backend.create(Ticket(title='Same title'))
    backend.create(Ticket(title='Same title'))
    assert len(set(keys)) == 2


@pytest.mark.parametrize('recovered', [False, True])
def test_cli_recovered_receipt_records_session_only_after_confirmation(monkeypatch, isolated_state, capsys, recovered):
    from tasklib import cli, read_tracker, session
    from tasklib.backends import backend_coordinate
    backend = configured_backend()
    monkeypatch.setenv('TASK_SESSION', 'recovery-session')
    monkeypatch.setattr('tasklib.backends.get_backend', lambda cfg, env=None: backend)
    monkeypatch.setattr(backend, '_label_ids', lambda labels: [])
    monkeypatch.setattr(backend, 'session_tickets', lambda *args, **kwargs: [])
    calls = []
    def request(url, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1 or not recovered:
            raise transport.AmbiguousHttpError('lost response')
        return json.dumps({'id': 'receipt', 'key': 'HYP-42', 'title': 'Synthetic recovery',
                           'description': 'saved', 'labels': ['session:recovery-session']}).encode()
    monkeypatch.setattr('tasklib.backends.tasks.request_bytes', request)
    result = cli.main(['create', '--title', 'Synthetic recovery', '--what', 'Restore readable receipts',
                       '--why', 'Users need the receipt', '--impact', 'Users can see whether their ticket was created after a slow connection, so they can continue their work without accidentally submitting the same request twice.',
                       '--if-not-done', 'Users may create duplicate tickets.', '--acceptance', 'Return the saved receipt',
                       '--acceptance', 'Keep uncertain outcomes explicit', '--json'])
    output = capsys.readouterr().out
    assert len(calls) == 2, output
    if recovered:
        assert result == 0
        assert json.loads(output)['id'] == 'HYP-42'
        assert session.read_ids('recovery-session') == ['HYP-42']
        assert read_tracker.is_fresh('recovery-session', 'HYP-42', repo=backend_coordinate(backend)) is True
    else:
        assert result == 9
        assert 'may have been created' in output
        assert session.read_ids('recovery-session') == []
        assert read_tracker.is_fresh('recovery-session', 'HYP-42', repo=backend_coordinate(backend)) is False
