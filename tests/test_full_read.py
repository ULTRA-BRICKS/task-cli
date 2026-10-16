"""Full-read fidelity and output receipts using synthetic documents only.

Exercises cmd_read with isolated state; no provider or notifier is contacted.
"""
import json
from dataclasses import replace

import pytest

from tasklib import cli
from tasklib.model import Ticket
from tasklib.render import parse, render

DOCUMENT = 'Provenance café 🧪\n# Custom scope\n```md\n## What\nverbatim\n```\n### Evidence\n\n  spacing  \n'


@pytest.fixture
def read_ticket(monkeypatch, fake_backend, isolated_state):
    monkeypatch.setattr('tasklib.backends.get_backend', lambda cfg, env=None: fake_backend)
    monkeypatch.setenv('TASK_SESSION', 'full-read-test')
    ticket = parse(DOCUMENT, Ticket(id='#1', title='Synthetic document'))
    fake_backend._store['#1'] = ticket
    marks = []
    monkeypatch.setattr(cli, '_mark_ticket_read', lambda *args: marks.append(args))
    return fake_backend, ticket, marks


@pytest.mark.parametrize('machine', [False, True])
@pytest.mark.parametrize('document', [DOCUMENT, '', ' \n', render(Ticket(what='Canonical café'))])
def test_full_read_preserves_backend_document(read_ticket, capsys, machine, document):
    backend, ticket, marks = read_ticket
    backend._store['#1'] = parse(document, ticket)
    assert cli.main(['read', '#1'] + (['--json'] if machine else [])) == 0
    out = capsys.readouterr().out
    if machine:
        assert json.loads(out)['body'] == document
    else:
        # The original body is a contiguous, byte-for-byte block before discussion metadata.
        assert out.split('\n\n', 1)[1].startswith(document + '\n')
    assert len(marks) == 1


def test_synthetic_ticket_uses_documented_canonical_fallback(read_ticket, capsys):
    backend, ticket, marks = read_ticket
    backend._store['#1'] = Ticket(id='#1', what='Synthetic only')
    assert cli.main(['read', '#1', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['body'] == render(backend._store['#1'])
    assert len(marks) == 1


@pytest.mark.parametrize('machine', [False, True])
def test_read_flush_failure_never_mints_marker(read_ticket, monkeypatch, machine):
    backend, ticket, marks = read_ticket
    def fail_flush():
        raise BrokenPipeError('synthetic closed output')
    with monkeypatch.context() as patch:
        patch.setattr(cli.sys.stdout, 'flush', fail_flush)
        with pytest.raises(BrokenPipeError):
            cli.cmd_read(cli.build_parser().parse_args(['read', '#1'] + (['--json'] if machine else [])))
    assert marks == []


@pytest.mark.parametrize('count', [0, 13])
@pytest.mark.parametrize('machine', [False, True])
def test_complete_discussion_and_imported_attribution(read_ticket, capsys, count, machine):
    backend, ticket, marks = read_ticket
    comments = [dict(id=f'comment-{i}', author_id='import-user', author_name='Original Author',
                     author_source_id='source-person', source={'provider': 'linear', 'id': f'old-{i}'},
                     created_at='2026-01-01T00:00:00Z', updated_at='2026-01-02T00:00:00Z',
                     body=f'Comment {i}: café\n```text\n' + 'long detail\n' * 400 + '```')
                for i in range(count)]
    backend._store['#1'] = replace(ticket, comments=comments)
    assert cli.main(['read', '#1'] + (['--json'] if machine else [])) == 0
    out = capsys.readouterr().out
    if machine:
        result = json.loads(out)
        assert result['comments'] == comments
        assert result['comments_status'] == 'fetched'
    else:
        assert 'Comments (fetched' in out
        for c in comments:
            assert c['body'] in out
            for key in ('id', 'author_name', 'author_source_id', 'created_at', 'updated_at'):
                assert c[key] in out
    assert len(marks) == 1


def test_unfetched_discussion_is_not_zero(read_ticket, capsys):
    assert cli.main(['read', '#1', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['comments'] is None
    assert result['comments_status'] == 'not-fetched'


def test_truncated_discussion_is_explicit(read_ticket, capsys):
    backend, ticket, marks = read_ticket
    backend._store['#1'] = replace(ticket, comments=[{'id': 'partial', 'body': 'kept'}], comments_truncated=True)
    assert cli.main(['read', '#1', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['comments_status'] == 'truncated'


@pytest.mark.parametrize('machine', [False, True])
def test_discussion_write_failure_does_not_mint_marker(read_ticket, monkeypatch, machine):
    import builtins
    backend, ticket, marks = read_ticket
    backend._store['#1'] = replace(ticket, comments=[{'body': 'discussion trigger'}])
    original_print = builtins.print
    def fail_on_discussion(*args, **kwargs):
        if any('discussion trigger' in str(arg) for arg in args):
            raise BrokenPipeError('synthetic discussion output failure')
        return original_print(*args, **kwargs)
    monkeypatch.setattr(builtins, 'print', fail_on_discussion)
    with pytest.raises(BrokenPipeError):
        cli.cmd_read(cli.build_parser().parse_args(['read', '#1'] + (['--json'] if machine else [])))
    assert marks == []


def test_optional_comment_fields_and_list_output(read_ticket, capsys):
    backend, ticket, marks = read_ticket
    backend._store['#1'] = replace(ticket, comments=[{'body': 'Minimal comment'}, {}])
    assert cli.main(['read', '#1']) == 0
    assert 'Minimal comment' in capsys.readouterr().out
    assert cli.main(['list', '--json']) == 0
    listing = json.loads(capsys.readouterr().out)
    assert 'comments' not in listing[0]
    assert 'comments_status' not in listing[0]
