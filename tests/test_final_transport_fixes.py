"""Committed mutations must remain uncertain across CLI dispatch and redirects.

Real adapters and urllib handlers consume synthetic responses, without provider I/O.
"""
import http.client
import io
import json
import socket
import ssl
import urllib.error
import urllib.request
from email.message import Message

import pytest

from tasklib import cli, read_tracker, session
from tasklib.backends import backend_coordinate
from tasklib.backends import http as transport
from tasklib.model import Criterion, Ticket
from tasklib.render import render
from tests.test_review_corrections import TOKEN, tasks_backend


@pytest.mark.parametrize('command', [
    ['status', 'HYP-1', 'in-progress'],
    ['status', 'HYP-1', 'done'],
    ['change', 'HYP-1', '--what', 'Preserve the saved description'],
    ['change', 'HYP-1', '--done'],
    ['done', 'HYP-1'],
    ['check', 'HYP-1', '1', '--proof', 'https://example.test/c.png'],
])
@pytest.mark.parametrize('failure', ['receipt', 'headers'])
def test_committed_patch_returns_uncertainty_without_success(monkeypatch, isolated_state, capsys, command, failure):
    backend = tasks_backend()
    backend._statuses += [
        {'id': 'started', 'category': 'started', 'name': 'In Progress', 'team_id': 'team'},
        {'id': 'done', 'category': 'completed', 'name': 'Done', 'team_id': 'team'},
    ]
    monkeypatch.setenv('TASK_SESSION', 'uncertainty')
    monkeypatch.setattr('tasklib.backends.get_backend', lambda cfg, env=None: backend)
    ticket = Ticket(title='Preserve ticket descriptions', what='Preserve the complete description',
                    why='Users need their saved work',
                    user_impact='Users can read their complete ticket descriptions, so they can continue their work without losing the details they recorded.',
                    cost_of_inaction='Users lose important details from their work.',
                    acceptance=[Criterion('Preserve saved text', checked=True, proof='https://example.test/a.png'),
                                Criterion('Keep errors explicit', checked=True, proof='https://example.test/b.png')])
    if command[0] == 'check':
        ticket.acceptance[0].checked = False
        ticket.acceptance[0].proof = ''
    row = {'id': 'issue', 'key': 'HYP-1', 'title': ticket.title, 'description': render(ticket),
           'version': 1, 'status': {'category': 'unstarted'}, 'comments': []}
    committed = []

    class Response:
        status = 200
        def __init__(self, body):
            self.body = body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return self.body

    def open_request(req, **kwargs):
        if req.get_method() == 'GET':
            return Response(json.dumps(row).encode())
        assert req.get_method() == 'PATCH'
        payload = json.loads(req.data)
        row.update(payload)
        committed.append(payload)
        if failure == 'headers':
            raise http.client.RemoteDisconnected(TOKEN)
        return Response(b'not JSON')

    monkeypatch.setattr(transport._SAME_HOST_OPENER, 'open', open_request)
    # Earn the read-before-edit credential through the real read command.
    had_read = '--what' in command
    if had_read:
        assert cli.main(['read', 'HYP-1']) == 0
        capsys.readouterr()
    effects = []
    monkeypatch.setattr(cli, '_mark_ticket_read', lambda *a, **kw: effects.append('marker'))
    monkeypatch.setattr(cli, '_record_session_touch', lambda *a, **kw: effects.append('session'))
    monkeypatch.setattr(cli, '_notify_mutation', lambda *a, **kw: effects.append('notify'))
    events = []
    monkeypatch.setattr('tasklib.logging.log_event', lambda event, **kw: events.append(event))
    assert cli.main(command) == 9
    output = capsys.readouterr().out
    assert 'ambiguous:' in output and 'before retrying' in output
    assert TOKEN not in output and 'updated HYP-1' not in output and '→' not in output
    assert len(committed) == 1
    assert effects == []
    assert not any(event.startswith(('ticket.', 'criterion.')) for event in events), events
    assert session.read_ids('uncertainty') == []
    assert read_tracker.is_fresh('uncertainty', 'HYP-1', repo=backend_coordinate(backend)) is had_read


@pytest.mark.parametrize('helper,guarded', [(transport.request_json, False), (transport.request_bytes, False), (transport.request_bytes, True)])
@pytest.mark.parametrize('method', ['POST', 'GET', 'HEAD'])
@pytest.mark.parametrize('failure', ['refused', 'dns', 'tls', '404', 'malformed', 'unsafe'])
def test_redirect_failure_retains_original_write_intent(monkeypatch, helper, guarded, method, failure):
    calls = []

    class Wire(urllib.request.HTTPHandler):
        def http_open(self, req):
            calls.append(req.get_method())
            if len(calls) == 1:
                headers = Message()
                headers['Location'] = ('http://[bad' if failure == 'malformed' else
                                       'file:///private' if failure == 'unsafe' else '/receipt')
                response = urllib.response.addinfourl(io.BytesIO(b''), headers, req.full_url, 303)
                response.msg = 'See Other'
                return response
            if failure == '404':
                raise urllib.error.HTTPError(req.full_url, 404, TOKEN, {}, io.BytesIO(TOKEN.encode()))
            errors = {'refused': ConnectionRefusedError(TOKEN), 'dns': socket.gaierror(TOKEN),
                      'tls': ssl.SSLCertVerificationError(TOKEN)}
            raise urllib.error.URLError(errors[failure])

    opener = urllib.request.build_opener(Wire(), transport._SameHostRedirectHandler() if guarded else urllib.request.HTTPRedirectHandler())
    if guarded:
        monkeypatch.setattr(transport, '_SAME_HOST_OPENER', opener)
    else:
        monkeypatch.setattr(urllib.request, 'urlopen', opener.open)
    options = {'same_host_redirects_only': True} if guarded else {}
    expected = transport.AmbiguousHttpError if method == 'POST' else transport.HttpError
    with pytest.raises(expected) as caught:
        helper('http://example.test/issues', method=method, **options)
    # CPython's supported versions differ on HEAD -> 303 (GET on 3.11/3.12,
    # HEAD on newer versions). This adapter delegates method selection to urllib;
    # compare with the independent stock handler, not a fixed runtime assumption.
    stock_request = urllib.request.HTTPRedirectHandler().redirect_request(
        urllib.request.Request('http://example.test/issues', method=method),
        None, 303, 'See Other', {}, 'http://example.test/receipt',
    )
    expected_calls = [method] if failure in {'malformed', 'unsafe'} else [
        method, stock_request.get_method(),
    ]
    assert calls == expected_calls
    assert TOKEN not in str(caught.value)
    if method == 'POST':
        assert 'may have' in str(caught.value) and 'before sending' not in str(caught.value)


@pytest.mark.parametrize('helper', [transport.request_json, transport.request_bytes])
@pytest.mark.parametrize('error', [ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError])
def test_direct_presend_failure_is_still_clean(monkeypatch, helper, error):
    calls = []
    def open_request(req, **kwargs):
        calls.append(req.get_method())
        raise urllib.error.URLError(error(TOKEN))
    monkeypatch.setattr(urllib.request, 'urlopen', open_request)
    with pytest.raises(transport.HttpError, match='before sending'):
        helper('https://example.test/issues', method='POST')
    assert calls == ['POST']


@pytest.mark.parametrize('recovered', [False, True])
def test_redirected_create_recovery_is_keyed_and_bounded(monkeypatch, recovered):
    writes = []
    operations = {}

    class Wire(urllib.request.HTTPSHandler):
        def https_open(self, req):
            headers = Message()
            if req.get_method() == 'GET':
                raise urllib.error.HTTPError(req.full_url, 404, TOKEN, {}, io.BytesIO())
            key = req.get_header('Idempotency-key')
            writes.append((key, req.data))
            operations.setdefault(key, {'id': 'issue', 'key': 'HYP-1'})
            if recovered and len(writes) == 2:
                body, status = json.dumps(operations[key]).encode(), 201
            else:
                headers['Location'] = '/receipt'
                body, status = b'', 303
            response = urllib.response.addinfourl(io.BytesIO(body), headers, req.full_url, status)
            response.msg = 'Synthetic response'
            return response

    monkeypatch.setattr(transport, '_SAME_HOST_OPENER', urllib.request.build_opener(Wire(), transport._SameHostRedirectHandler()))
    from tasklib.backends import AmbiguousBackendError
    if recovered:
        assert tasks_backend().create(Ticket(title='Recover receipt')).id == 'HYP-1'
    else:
        with pytest.raises(AmbiguousBackendError, match='recovery exhausted'):
            tasks_backend().create(Ticket(title='Recover receipt'))
    assert len(writes) == 2 and writes[0] == writes[1]
    assert writes[0][0].startswith('task-create:')
    assert len(operations) == 1


@pytest.mark.parametrize('helper', [transport.request_json, transport.request_bytes])
@pytest.mark.parametrize('failure', ['refused', '404'])
def test_loopback_committed_post_redirect_failure(helper, failure):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            calls.append('POST')
            # Closing the listening socket proves refusal belongs to the later GET.
            if failure == 'refused':
                self.server.server_close()
            self.send_response(303)
            self.send_header('Location', '/receipt')
            self.end_headers()
        def do_GET(self):
            calls.append('GET')
            self.send_error(404)

    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.handle_request if failure == 'refused' else server.serve_forever)
    worker.start()
    try:
        with pytest.raises(transport.AmbiguousHttpError):
            helper(f'http://127.0.0.1:{server.server_port}/issues', method='POST', timeout=2)
        assert calls == (['POST'] if failure == 'refused' else ['POST', 'GET'])
    finally:
        if failure != 'refused':
            server.shutdown()
        server.server_close()
        worker.join(timeout=3)
