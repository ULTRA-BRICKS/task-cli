"""A human-readable key is not a cross-tracker identity."""
from tasklib.cli import _route_id_to_project
from tasklib.projects import projects_from_config


def test_same_key_in_linear_and_tasks_is_not_automatically_merged():
    projects = projects_from_config({'projects': [
        {'name': 'Conloca', 'linear': {'team': 'GL', 'project': 'conloca'}},
        {'name': 'Other store', 'tasks': {'team': 'GL', 'url': 'https://tasks.example.test'}},
    ]})
    assert _route_id_to_project('GL-264', projects) is None
    assert _route_id_to_project('GL-264', list(reversed(projects))) is None


def test_explicit_linear_registry_remains_unambiguous():
    projects = projects_from_config({'projects': [
        {'name': 'Conloca', 'linear': {'team': 'GL', 'project': 'conloca'}},
    ]})
    resolved = _route_id_to_project('GL-264', projects)
    assert resolved is not None
    assert resolved.coordinate == 'linear:GL/conloca'


def test_repo_config_beats_workspace_default(tmp_path, monkeypatch):
    from tasklib.config import load

    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    workspace = tmp_path / 'work'
    repo = workspace / 'conloca'
    repo.mkdir(parents=True)
    (repo / '.git').mkdir()
    (workspace / 'rig.yaml').write_text(
        'version: 1\ntask:\n  backend: tasks\n  tasks:\n'
        '    url: https://tasks.example.test\n    team: HYP\n'
    )
    (repo / 'task.yaml').write_text(
        'version: 1\nbackend: linear\nlinear:\n  team: GL\n  project: conloca\n'
    )
    sibling = workspace / 'other-repo'
    sibling.mkdir()
    (sibling / '.git').mkdir()
    inherited = load(sibling)
    assert inherited.backend == 'tasks'
    assert inherited.section('tasks')['team'] == 'HYP'
    configured = load(repo)
    assert configured.backend == 'linear'
    assert configured.section('linear')['team'] == 'GL'
    assert configured.section('linear')['project'] == 'conloca'
    assert configured.data['enforce'] == inherited.data['enforce']


def test_missing_linear_access_does_not_fall_back_to_tasks(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from tasklib.backends import get_backend
    from tasklib.credentials import CredentialError

    calls = []
    def unavailable(env=None):
        calls.append('linear')
        raise CredentialError('Linear access is unavailable')

    def wrong_store(*args, **kwargs):
        calls.append('tasks')
        raise AssertionError('An explicit Linear route must not fall back to Tasks')

    monkeypatch.setattr('tasklib.credentials.linear_key', unavailable)
    monkeypatch.setattr('tasklib.backends.tasks.TasksBackend.from_config', wrong_store)
    config = SimpleNamespace(backend='linear', section=lambda name: {'team': 'GL', 'project': 'conloca'})
    with pytest.raises(CredentialError, match='Linear access'):
        get_backend(config, env={})
    assert calls == ['linear']
