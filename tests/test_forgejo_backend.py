"""Hermetic Forgejo backend coverage (UB-25) — an in-memory fake of the Forgejo 16 REST API.

No live requests, no real credentials: ``request_bytes`` inside ``tasklib.backends.forgejo`` is
replaced by :class:`FakeForgejo`, which enforces the same-origin/auth invariants on every call
and simulates the endpoint shapes confirmed against the live server's Swagger (Forgejo 16.0.3):
``labels`` on create are integer ids, ``EditIssueOption`` has NO ``labels`` (label changes go
through ``PUT /issues/{index}/labels``), plain issues carry ``"pull_request": null``, the issue
list is ``page``/``limit`` paginated, and uploads are multipart ``attachment`` form fields.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tasklib.backends import BackendError, TicketBackend, backend_coordinate, get_backend
from tasklib.backends.forgejo import ForgejoBackend, _git_origin_url_from_config
from tasklib.config import ConfigError, load, validate
from tasklib.credentials import CredentialError, forgejo_token
from tasklib.model import State, Ticket
from tasklib.render import render

ORIGIN = "https://forge.example.test"
TOKEN = "dummy-forgejo-value"
PAGE_CAP = 2  # the fake server's MAX_RESPONSE_ITEMS: it silently caps `limit` like Forgejo does


def _issue(number, title, *, body="", labels=(), state="open", pr=False, repo="acme/web", due=None):
    return {
        "due_date": due,
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "labels": [{"id": lid, "name": name} for lid, name in labels],
        "html_url": f"{ORIGIN}/{repo}/{'pulls' if pr else 'issues'}/{number}",
        "user": {"login": "alice"},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "closed_at": None,
        "pull_request": {"merged": False} if pr else None,
    }


class FakeForgejo:
    """In-memory Forgejo: one repo's issues/labels/comments/assets, plus a call log."""

    def __init__(self, repo="acme/web"):
        self.repo = repo
        self.calls: list[tuple[str, str, object]] = []
        self.labels: dict[str, int] = {"agent": 1, "status:todo": 2, "bug": 3}
        self.issues: dict[int, dict] = {}
        self.comments: list[tuple[int, str]] = []
        self.assets: list[tuple[int, str, bytes]] = []
        self.blobs: dict[str, bytes] = {}
        self.reflect_token_in_errors = False

    def label_rows(self, names):
        return [(self.labels[n], n) for n in names]

    def add(self, row):
        self.issues[row["number"]] = row
        return row

    # the request_bytes stand-in ------------------------------------------------------------
    def __call__(self, url, *, method="GET", headers=None, data=None, **options):
        assert url.startswith(ORIGIN + "/"), f"request left the configured origin: {url}"
        assert headers["Authorization"] == f"token {TOKEN}"
        assert options.get("same_host_redirects_only") is True, "credentialed call must pin redirects"
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        payload = data
        if data is not None and headers.get("Content-Type") == "application/json":
            payload = json.loads(data)
        self.calls.append((method, parts.path, payload if method != "GET" else query))
        result = self._route(method, parts.path, query, payload, headers)
        if isinstance(result, bytes):
            return result
        return b"" if result is None else json.dumps(result).encode()

    def _route(self, method, path, query, payload, headers):
        base = f"/api/v1/repos/{self.repo}"
        if path.startswith("/attachments/") and method == "GET":
            return self.blobs[path.rsplit("/", 1)[1]]
        if path == "/api/v1/user":
            return {"login": "alice", "id": 7}
        if path == f"{base}/labels" and method == "GET":
            return [{"id": lid, "name": n} for n, lid in self.labels.items()]
        if path == f"{base}/labels" and method == "POST":
            lid = max(self.labels.values()) + 1
            self.labels[payload["name"]] = lid
            return {"id": lid, "name": payload["name"]}
        if path == f"{base}/issues" and method == "GET":
            return self._list(query)
        if path == f"{base}/issues" and method == "POST":
            number = max(self.issues, default=0) + 1
            names = {lid: n for n, lid in self.labels.items()}
            labels = [(lid, names[lid]) for lid in payload.get("labels", [])]
            row = _issue(number, payload["title"], body=payload["body"], labels=labels,
                         due=payload.get("due_date"), state="closed" if payload.get("closed") else "open")
            return self.add(row)
        if path.startswith(f"{base}/issues/"):
            return self._issue_route(method, path[len(f"{base}/issues/"):], query, payload, headers)
        raise AssertionError(f"unexpected {method} {path}")

    def _list(self, query):
        rows = sorted(self.issues.values(), key=lambda r: -r["number"])
        if query.get("type") == "issues":
            rows = [r for r in rows if not r["pull_request"]]
        if "q" in query:
            rows = [r for r in rows if query["q"].lower() in (r["title"] + r["body"]).lower()]
        limit = min(int(query.get("limit", PAGE_CAP)), PAGE_CAP)
        page = int(query.get("page", 1))
        return rows[(page - 1) * limit : page * limit]

    def _issue_route(self, method, rest, query, payload, headers):
        number_text, _, tail = rest.partition("/")
        row = self.issues.get(int(number_text))
        if row is None:
            raise_http(404, reflect=TOKEN if self.reflect_token_in_errors else "")
        if tail == "" and method == "GET":
            return row
        if tail == "" and method == "PATCH":
            assert "labels" not in payload, "EditIssueOption has no labels property"
            row.update({k: v for k, v in payload.items() if k in ("title", "body", "state", "due_date")})
            if payload.get("unset_due_date"):
                row["due_date"] = None
            row["updated_at"] = "2026-09-03T00:00:00Z"
            return row
        if tail == "labels" and method == "PUT":
            names = {lid: n for n, lid in self.labels.items()}
            row["labels"] = [{"id": lid, "name": names[lid]} for lid in payload["labels"]]
            return row["labels"]
        if tail == "comments" and method == "POST":
            self.comments.append((row["number"], payload["body"]))
            return {"id": len(self.comments), "body": payload["body"]}
        if tail == "assets" and method == "POST":
            assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
            assert b'name="attachment"' in payload
            self.assets.append((row["number"], query["name"], payload))
            uuid = f"uuid-{len(self.assets)}"
            self.blobs[uuid] = payload.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            return {"id": len(self.assets), "name": query["name"], "uuid": uuid,
                    "browser_download_url": f"{ORIGIN}/attachments/{uuid}"}
        raise AssertionError(f"unexpected {method} issue route {rest}")


def raise_http(status, reflect=""):
    from tasklib.backends.http import HttpError

    raise HttpError(status, f"HTTP {status}", '{"message":"not found", "echo": "token %s"}' % reflect)


@pytest.fixture
def forge(monkeypatch):
    fake = FakeForgejo()
    monkeypatch.setattr("tasklib.backends.forgejo.request_bytes", fake)
    return fake


def backend(repo="acme/web"):
    owner, name = repo.split("/")
    return ForgejoBackend(url=ORIGIN, owner=owner, repo=name, token=TOKEN)


# ── protocol + reads ───────────────────────────────────────────────────────────────────


def test_protocol_and_token_never_in_repr(forge):
    b = backend()
    assert isinstance(b, TicketBackend)
    assert b.name == "forgejo"
    assert TOKEN not in repr(b)
    assert b.current_user() == "alice"


def test_get_parses_row(forge):
    forge.add(_issue(5, "Five", body="plain body", labels=forge.label_rows(["bug", "status:todo"])))
    t = backend().get("#5")
    assert t.id == "#5" and t.title == "Five" and t.state == State.TODO
    assert t.labels == ["bug"] and t.raw_body == "plain body"
    assert t.url == f"{ORIGIN}/acme/web/issues/5"


def test_get_refuses_a_pull_request_number(forge):
    forge.add(_issue(6, "A PR", pr=True))
    with pytest.raises(BackendError, match="pull request"):
        backend().get("6")


def test_list_paginates_and_excludes_pull_requests(forge):
    for n in range(1, 6):
        forge.add(_issue(n, f"issue {n}", pr=(n == 4)))
    tickets = backend().list(limit=10)
    assert [t.id for t in tickets] == ["#5", "#3", "#2", "#1"]
    lists = [c for c in forge.calls if c[1].endswith("/issues") and c[0] == "GET"]
    assert all(c[2]["type"] == "issues" for c in lists), "server-side PR exclusion"
    assert len(lists) >= 2, "must follow pages past the server's silent limit cap"


def test_list_skips_pr_rows_even_if_server_ignores_type_filter(forge, monkeypatch):
    forge.add(_issue(1, "real issue"))
    forge.add(_issue(2, "sneaky PR", pr=True))
    original = forge._list
    monkeypatch.setattr(forge, "_list", lambda q: original({k: v for k, v in q.items() if k != "type"}))
    assert [t.id for t in backend().list(limit=10)] == ["#1"]


def test_list_state_filter_scans_past_non_matching_pages(forge):
    forge.add(_issue(1, "old todo"))
    for n in range(2, 6):
        forge.add(_issue(n, f"done {n}", state="closed"))
    todo = backend().list(state=State.TODO, limit=1)
    assert [t.id for t in todo] == ["#1"]


def test_search_uses_q_and_excludes_prs(forge):
    forge.add(_issue(1, "needle issue"))
    forge.add(_issue(2, "needle PR", pr=True))
    forge.add(_issue(3, "hay"))
    assert [t.id for t in backend().search("needle")] == ["#1"]


# ── writes ─────────────────────────────────────────────────────────────────────────────


def _ticket(**kw):
    base = dict(title="New", what="Do it", why="because", user_impact="users",
                cost_of_inaction="pain", acceptance=["works"], labels=["bug"])
    base.update(kw)
    return Ticket(**base)


def test_create_sends_label_ids_and_creates_missing_managed_labels(forge):
    b = ForgejoBackend(url=ORIGIN, owner="acme", repo="web", token=TOKEN, default_labels=("agent",))
    t = b.create(_ticket(labels=["bug", "brand-new"]))
    post = next(c for c in forge.calls if c[0] == "POST" and c[1].endswith("/issues"))
    assert all(isinstance(x, int) for x in post[2]["labels"])
    assert "brand-new" in forge.labels
    assert t.id == "#1" and set(t.labels) == {"agent", "bug", "brand-new"}


def test_update_metadata_only_never_sends_body_and_uses_labels_endpoint(forge):
    custom = "# Imported\n\nFree-form **markdown** the template does not know.\n"
    forge.add(_issue(8, "Imported", body=custom, labels=forge.label_rows(["status:todo"])))
    b = backend()
    t = b.get("#8")
    t.labels = ["bug"]
    t.title = "Imported (renamed)"
    b.update(t)
    patch = next(c for c in forge.calls if c[0] == "PATCH")
    assert patch[2] == {"title": "Imported (renamed)"}
    put = next(c for c in forge.calls if c[0] == "PUT")
    assert put[1].endswith("/issues/8/labels")
    assert forge.issues[8]["body"] == custom
    assert {lbl["name"] for lbl in forge.issues[8]["labels"]} == {"bug", "status:todo"}


def test_update_refuses_lossy_body_rewrite_before_any_write(forge):
    custom = "# Imported\n\nFree-form text.\n"
    forge.add(_issue(9, "Imported", body=custom))
    b = backend()
    t = b.get("#9")
    t.what = "a structured rewrite"
    with pytest.raises(BackendError, match="refuses to overwrite"):
        b.update(t)
    assert [c for c in forge.calls if c[0] != "GET"] == []
    assert forge.issues[9]["body"] == custom


def test_update_managed_body_change_is_sent(forge):
    managed = render(_ticket())
    forge.add(_issue(10, "Managed", body=managed))
    b = backend()
    t = b.get("#10")
    t.what = "changed what"
    b.update(t)
    patch = next(c for c in forge.calls if c[0] == "PATCH")
    assert "changed what" in patch[2]["body"]


def test_transition_swaps_status_label_and_closes_without_body(forge):
    custom = "hand-written body"
    forge.add(_issue(11, "T", body=custom, labels=forge.label_rows(["bug", "status:todo"])))
    t = backend().transition("#11", State.DONE)
    patch = next(c for c in forge.calls if c[0] == "PATCH")
    assert patch[2] == {"state": "closed"}
    names = {lbl["name"] for lbl in forge.issues[11]["labels"]}
    assert names == {"bug", "status:done"}
    assert forge.issues[11]["body"] == custom
    assert t.state == State.DONE


def test_transition_in_progress_keeps_issue_open_no_patch_needed(forge):
    forge.add(_issue(12, "T", labels=forge.label_rows(["status:todo"])))
    backend().transition("12", State.IN_PROGRESS)
    assert not any(c[0] == "PATCH" for c in forge.calls)
    assert {lbl["name"] for lbl in forge.issues[12]["labels"]} == {"status:in-progress"}


def test_comment_posts_body_verbatim(forge):
    forge.add(_issue(13, "C"))
    backend().comment("#13", "hello **world**")
    assert forge.comments == [(13, "hello **world**")]


def test_attach_native_uploads_multipart_and_comments_link(forge, tmp_path):
    forge.add(_issue(14, "A"))
    shot = tmp_path / "shot [1].png"
    shot.write_bytes(b"\x89PNG-bytes")
    url = backend().attach("#14", str(shot))
    assert url == f"{ORIGIN}/attachments/uuid-1"
    assert forge.assets[0][0] == 14 and b"\x89PNG-bytes" in forge.assets[0][2]
    ((number, text),) = forge.comments
    alt = text.split("](")[0].split("![", 1)[1]
    assert url in text and "[" not in alt and "]" not in alt


def test_attach_link_mode_refuses_local_path_without_calls(forge, tmp_path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"x")
    b = ForgejoBackend(url=ORIGIN, owner="acme", repo="web", token=TOKEN, attachment_mode="link")
    with pytest.raises(BackendError, match="attachment_mode=link"):
        b.attach("#1", str(shot))
    assert forge.calls == []


def test_fetch_attachment_only_sends_token_to_own_origin(forge):
    b = backend()
    assert b.is_native_attachment_url(f"{ORIGIN}/attachments/x")
    assert not b.is_native_attachment_url("https://evil.example/attachments/x")
    with pytest.raises(BackendError, match="origin"):
        b.fetch_attachment_bytes("https://evil.example/attachments/x")
    assert forge.calls == []


# ── ids, repo scoping, coordinates ───────────────────────────────────────────────────────


def test_repo_qualified_id_for_this_repo_is_accepted(forge):
    forge.add(_issue(3, "Three"))
    assert backend().get("acme/web#3").id == "#3"


def test_repo_qualified_id_for_another_repo_is_refused_before_http(forge):
    with pytest.raises(BackendError, match="acme/other"):
        backend().get("acme/other#3")
    assert forge.calls == []


@pytest.mark.parametrize("bad", ["HYP-3", "##3", "# 3", "3a", "", "../3"])
def test_malformed_ids_fail_closed_before_http(forge, bad):
    with pytest.raises(BackendError):
        backend().get(bad)
    assert forge.calls == []


def test_same_number_in_two_repos_is_two_distinct_tickets(monkeypatch):
    web, api = FakeForgejo("acme/web"), FakeForgejo("acme/api")
    web.add(_issue(1, "web one", repo="acme/web"))
    api.add(_issue(1, "api one", repo="acme/api"))

    def route(url, **kw):
        return (api if "/repos/acme/api/" in url else web)(url, **kw)

    monkeypatch.setattr("tasklib.backends.forgejo.request_bytes", route)
    b1, b2 = backend("acme/web"), backend("acme/api")
    t1, t2 = b1.get("#1"), b2.get("#1")
    assert (t1.title, t2.title) == ("web one", "api one")
    assert t1.url != t2.url
    assert backend_coordinate(b1) != backend_coordinate(b2)
    assert backend_coordinate(b1) == f"forgejo:{ORIGIN}/acme/web"


def test_missing_issue_is_a_clean_backend_error(forge):
    with pytest.raises(BackendError, match="404"):
        backend().get("#99")


def test_repo_without_issue_tracker_is_a_clean_backend_error(monkeypatch):
    def disabled(url, **kw):
        raise_http(404)

    monkeypatch.setattr("tasklib.backends.forgejo.request_bytes", disabled)
    with pytest.raises(BackendError, match="404"):
        backend().list()


# ── origin + credential security ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    ["http://forge.example.test", "https://u:p@forge.example.test", "https://forge.example.test/api/v1",
     "https://forge.example.test/?q=x", "file:///tmp/x", "not a url"],
)
def test_unsafe_origins_rejected(url):
    with pytest.raises(BackendError):
        ForgejoBackend(url=url, owner="acme", repo="web", token=TOKEN)


def test_http_errors_never_echo_the_token(forge):
    forge.reflect_token_in_errors = True  # a server that reflects the Authorization header
    with pytest.raises(BackendError) as info:
        backend().get("#1")
    assert "404" in str(info.value)
    assert TOKEN not in str(info.value) and "<redacted>" in str(info.value)


@pytest.mark.parametrize("url", ["https://user:hunter2-secret@forge.example.test",
                                 "https://forge.example.test/?access_token=hunter2-secret"])
def test_origin_errors_never_echo_credentials(url):
    with pytest.raises(BackendError) as info:
        ForgejoBackend(url=url, owner="acme", repo="web", token=TOKEN)
    assert "hunter2-secret" not in str(info.value)


def test_config_url_errors_never_echo_credentials():
    with pytest.raises(ConfigError) as info:
        validate(_cfg(url="https://user:hunter2-secret@git.hyperide.ai", repo="acme/web"))
    assert "hunter2-secret" not in str(info.value)


def test_native_attachment_url_rejects_userinfo_on_the_same_host():
    assert not backend().is_native_attachment_url("https://x:y@forge.example.test/attachments/a")


def test_full_issue_url_accepted_only_for_this_origin_and_repo(forge):
    forge.add(_issue(3, "Three"))
    assert backend().get(f"{ORIGIN}/acme/web/issues/3").id == "#3"
    for other in ("https://evil.example/acme/web/issues/3", f"{ORIGIN}/acme/other/issues/3",
                  "https://u:p@forge.example.test/acme/web/issues/3"):
        with pytest.raises(BackendError):
            backend().get(other)
    assert len([c for c in forge.calls if c[0] == "GET"]) == 1


def test_redirect_to_other_host_is_refused_by_the_real_opener():
    # The backend relies on http._SameHostRedirectHandler (same_host_redirects_only=True, which
    # the fake server above asserts on EVERY call); pin the handler's cross-host refusal here.
    import urllib.error
    import urllib.request

    from tasklib.backends.http import _SameHostRedirectHandler

    req = urllib.request.Request(f"{ORIGIN}/api/v1/user", headers={"Authorization": "token x"})
    with pytest.raises(urllib.error.HTTPError, match="refused unsafe redirect"):
        _SameHostRedirectHandler().redirect_request(req, None, 302, "Found", {}, "https://evil.example/")


def _keys_home(tmp_path, host):
    home = tmp_path / "home"
    store = home / "Library" / "Application Support" / "forgejo-cli.forgejo-cli"
    store.mkdir(parents=True)
    (store / "keys.json").write_text(
        json.dumps({"hosts": {host: {"type": "Application", "token": TOKEN}}, "aliases": {}}),
        encoding="utf-8",
    )
    return {"HOME": str(home)}


def test_credential_harvest_from_fj_store_is_bound_to_the_configured_host(tmp_path):
    env = _keys_home(tmp_path, "forge.example.test")
    creds = forgejo_token(ORIGIN, env)
    assert creds.token == TOKEN and TOKEN not in creds.source
    with pytest.raises(CredentialError):
        forgejo_token("https://other.example.test", env)


def test_credential_env_var_wins(tmp_path):
    env = {**_keys_home(tmp_path, "forge.example.test"), "FORGEJO_TOKEN": "dummy-env-value",
           "FORGEJO_URL": ORIGIN}
    assert forgejo_token(ORIGIN, env).token == "dummy-env-value"


def test_fj_aliases_are_never_followed(tmp_path):
    env = _keys_home(tmp_path, "forge.example.test")
    store = Path(env["HOME"]) / "Library" / "Application Support" / "forgejo-cli.forgejo-cli" / "keys.json"
    data = json.loads(store.read_text())
    data["aliases"] = {"alias.example.test": "forge.example.test"}
    store.write_text(json.dumps(data))
    with pytest.raises(CredentialError):
        forgejo_token("https://alias.example.test", env)


def test_get_backend_constructs_forgejo_from_config(forge):
    cfg = SimpleNamespace(
        backend="forgejo",
        repo_root=Path("."),
        section=lambda name: {"url": ORIGIN, "repo": "acme/web", "attachment_mode": "native"},
    )
    b = get_backend(cfg, env={"FORGEJO_TOKEN": TOKEN, "FORGEJO_URL": ORIGIN})
    assert isinstance(b, ForgejoBackend) and (b.owner, b.repo) == ("acme", "web")


# ── config contract ────────────────────────────────────────────────────────────────────


def _cfg(**forgejo):
    return {"version": 1, "backend": "forgejo", "forgejo": forgejo}


def test_config_accepts_the_documented_contract():
    validate(_cfg(url="https://git.hyperide.ai", repo="ultrabricks/task-cli", attachment_mode="native"))
    validate(_cfg(url="https://git.hyperide.ai", repo="auto"))


@pytest.mark.parametrize(
    "section",
    [
        {"repo": "acme/web"},  # url is required for the forgejo backend
        {"url": "http://git.hyperide.ai", "repo": "acme/web"},
        {"url": "https://git.hyperide.ai/api/v1", "repo": "acme/web"},
        {"url": "https://git.hyperide.ai", "repo": "no-slash"},
        {"url": "https://git.hyperide.ai", "repo": "a/b/c"},
        {"url": "https://git.hyperide.ai", "repo": ["acme/web"]},
        {"url": "https://git.hyperide.ai", "repo": "acme/web", "attachment_mode": "carrier-pigeon"},
    ],
)
def test_config_rejects_malformed_forgejo_sections(section):
    with pytest.raises(ConfigError):
        validate(_cfg(**section))


def _git_repo(tmp_path, remote):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)
    return repo


def test_forgejo_repo_auto_with_origin_on_another_host_is_a_clean_error(tmp_path):
    repo = _git_repo(tmp_path, "https://github.com/acme/web")
    cfg = SimpleNamespace(backend="forgejo", repo_root=repo,
                          section=lambda name: {"url": ORIGIN, "repo": "auto"})
    with pytest.raises(BackendError, match="forge.example.test"):
        get_backend(cfg, env={"FORGEJO_TOKEN": TOKEN})


def test_forgejo_repo_auto_resolves_origin_on_the_configured_host(tmp_path, forge):
    repo = _git_repo(tmp_path, "https://someone@forge.example.test/acme/web.git")
    cfg = SimpleNamespace(backend="forgejo", repo_root=repo,
                          section=lambda name: {"url": ORIGIN, "repo": "auto"})
    b = get_backend(cfg, env={"FORGEJO_TOKEN": TOKEN, "FORGEJO_URL": ORIGIN})
    assert (b.owner, b.repo) == ("acme", "web")


def _workspace(tmp_path, ancestor_yaml):
    home = tmp_path / "home"
    repo = home / "ws" / "proj"
    repo.mkdir(parents=True)
    (home / "ws" / "rig.yaml").write_text(ancestor_yaml, encoding="utf-8")
    return home, repo


def test_ancestor_forgejo_repo_is_inherited_like_any_other_key(tmp_path):
    # UB-25 cross-repo contract (coordinator decision, shared with rig-cli): only code_prefix is
    # stripped from an ancestor; an explicit forgejo.repo is inherited normally.
    home, repo = _workspace(
        tmp_path,
        "task:\n  backend: forgejo\n  code_prefix: WS\n"
        "  forgejo:\n    url: https://git.hyperide.ai\n    repo: acme/canonical\n",
    )
    cfg = load(repo, include_global=False, env={"HOME": str(home)})
    assert cfg.backend == "forgejo"
    assert cfg.section("forgejo") == {**cfg.section("forgejo"), "url": "https://git.hyperide.ai",
                                      "repo": "acme/canonical"}
    assert "code_prefix" not in cfg.data


def test_repo_own_forgejo_repo_wins_over_the_ancestor(tmp_path):
    home, repo = _workspace(
        tmp_path, "task:\n  backend: forgejo\n  forgejo: {url: 'https://git.hyperide.ai', repo: acme/canonical}\n"
    )
    (repo / "rig.yaml").write_text("task:\n  forgejo: {repo: acme/proj}\n", encoding="utf-8")
    cfg = load(repo, include_global=False, env={"HOME": str(home)})
    assert cfg.section("forgejo")["repo"] == "acme/proj"
    assert cfg.section("forgejo")["url"] == "https://git.hyperide.ai"


def test_folder_wide_repo_auto_default_resolves_each_repo_origin(tmp_path, forge):
    home, repo = _workspace(
        tmp_path, "task:\n  backend: forgejo\n  forgejo: {url: 'https://forge.example.test', repo: auto}\n"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "git@forge.example.test:acme/proj.git"], check=True)
    cfg = load(repo, include_global=False, env={"HOME": str(home)})
    assert cfg.section("forgejo")["repo"] == "auto"
    b = get_backend(cfg, env={"FORGEJO_TOKEN": TOKEN, "FORGEJO_URL": ORIGIN})
    assert (b.owner, b.repo) == ("acme", "proj")


def test_rig_global_forgejo_section_cascades_like_any_other_key(tmp_path):
    home = tmp_path / "home"
    repo = home / "proj"
    repo.mkdir(parents=True)
    rig_global = tmp_path / "xdg" / "rig"
    rig_global.mkdir(parents=True)
    (rig_global / "config.yaml").write_text(
        "task:\n  backend: forgejo\n  forgejo: {url: 'https://git.hyperide.ai', repo: auto}\n",
        encoding="utf-8",
    )
    cfg = load(repo, env={"HOME": str(home), "XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert cfg.backend == "forgejo" and cfg.section("forgejo")["url"] == "https://git.hyperide.ai"


def test_ancestor_forgejo_section_with_unknown_key_fails_closed_naming_the_file(tmp_path):
    home = tmp_path / "home"
    repo = home / "ws" / "proj"
    repo.mkdir(parents=True)
    bad = home / "ws" / "rig.yaml"
    bad.write_text("task:\n  forgejo: {url: 'https://git.hyperide.ai', rep: typo/x}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="rep"):
        load(repo, include_global=False, env={"HOME": str(home)})


def test_env_token_is_bound_to_forgejo_url(tmp_path):
    env = {"HOME": str(tmp_path), "FORGEJO_TOKEN": "dummy-env-value"}
    with pytest.raises(CredentialError):
        forgejo_token(ORIGIN, env)  # no FORGEJO_URL → the ambient token is not sent anywhere
    with pytest.raises(CredentialError):
        forgejo_token(ORIGIN, {**env, "FORGEJO_URL": "https://other.example.test"})
    assert forgejo_token(ORIGIN, {**env, "FORGEJO_URL": ORIGIN + "/"}).token == "dummy-env-value"


@pytest.mark.parametrize(
    ("url", "origin"),
    [("https://Forge.Example.test:443/", "https://forge.example.test"),
     ("https://forge.example.test:8443", "https://forge.example.test:8443")],
)
def test_origin_port_normalization(url, origin):
    b = ForgejoBackend(url=url, owner="acme", repo="web", token=TOKEN)
    assert b.url == origin
    assert b.is_native_attachment_url(origin + "/attachments/x")
    assert not b.is_native_attachment_url("https://forge.example.test:9443/attachments/x")


def test_registry_and_live_backend_share_one_coordinate():
    from tasklib.projects import projects_from_config

    (proj,) = projects_from_config(
        {"projects": [{"forgejo": {"url": "https://Forge.Example.test:443/", "repo": "Acme/Web"}}]}
    )
    assert proj.coordinate == backend_coordinate(backend("Acme/Web"))



# ── stale reads, minimal writes, native due dates ───────────────────────────────────────


def test_update_refuses_when_the_issue_changed_since_it_was_read(forge):
    forge.add(_issue(20, "T", labels=forge.label_rows(["status:todo"])))
    b = backend()
    t = b.get("#20")
    forge.issues[20]["labels"].append({"id": 3, "name": "bug"})  # someone else labels it
    t.title = "mine"
    with pytest.raises(BackendError, match="changed on the server"):
        b.update(t)
    assert [c for c in forge.calls if c[0] != "GET"] == []
    assert forge.issues[20]["title"] == "T"


def test_title_only_update_does_not_replace_labels_or_state(forge):
    forge.add(_issue(21, "T", labels=forge.label_rows(["bug", "status:todo"])))
    b = backend()
    t = b.get("#21")
    t.title = "renamed"
    b.update(t)
    writes = [c for c in forge.calls if c[0] != "GET"]
    assert writes == [("PATCH", "/api/v1/repos/acme/web/issues/21", {"title": "renamed"})]


def test_native_due_date_wins_and_body_stays_verbatim(forge):
    body = "Imported text\n\n## Due\n2020-01-01\n"
    forge.add(_issue(22, "D", body=body, due="2026-10-05T12:00:00+02:00"))
    t = backend().get("#22")
    assert t.due == "2026-10-05" and t.raw_body == body


def test_due_only_update_and_clear_on_imported_body_never_touch_the_body(forge):
    body = "# Imported\n\nNot the managed template.\n"
    forge.add(_issue(23, "D", body=body))
    b = backend()
    t = b.get("#23")
    t.due = "2026-11-01"
    t = b.update(t)
    assert [c[2] for c in forge.calls if c[0] == "PATCH"] == [{"due_date": "2026-11-01T12:00:00Z"}]
    assert t.due == "2026-11-01"
    t.due = ""
    b.update(t)
    assert [c[2] for c in forge.calls if c[0] == "PATCH"][-1] == {"unset_due_date": True}
    assert forge.issues[23]["body"] == body and forge.issues[23]["due_date"] is None


def test_create_sends_native_due_date(forge):
    t = backend().create(_ticket(due="2026-12-24"))
    post = next(c for c in forge.calls if c[0] == "POST" and c[1].endswith("/issues"))
    assert post[2]["due_date"] == "2026-12-24T12:00:00Z" and t.due == "2026-12-24"


# ── pagination completeness ────────────────────────────────────────────────────────────


def test_state_filter_finds_a_match_beyond_100_scanned_issues(forge):
    forge.add(_issue(1, "the only open one"))
    for n in range(2, 140):
        forge.add(_issue(n, f"closed {n}", state="closed"))
    assert [t.id for t in backend().list(state=State.TODO, limit=5)] == ["#1"]


def test_multi_label_and_match_beyond_the_first_page(forge):
    forge.labels.update({"session:x": 10, "ui": 11})
    for n in range(10, 3, -1):
        forge.add(_issue(n, f"only one label {n}", labels=forge.label_rows(["ui"])))
    forge.add(_issue(1, "both", labels=forge.label_rows(["ui", "session:x"])))
    assert [t.id for t in backend().list(labels=["ui", "session:x"], limit=3)] == ["#1"]


def test_safety_cap_is_reported_as_truncation(forge, monkeypatch, capsys):
    monkeypatch.setattr("tasklib.backends.forgejo._MAX_PAGES", 2)
    for n in range(1, 10):
        forge.add(_issue(n, f"closed {n}", state="closed"))
    assert backend().list(state=State.TODO, limit=5) == []
    assert "truncated" in capsys.readouterr().err


# ── attachment readback + partial success ──────────────────────────────────────────────


def test_upload_is_verified_by_reading_the_bytes_back(forge, tmp_path):
    forge.add(_issue(30, "A"))
    shot = tmp_path / "a.png"
    shot.write_bytes(b"real-bytes")
    backend().attach("#30", str(shot))
    assert ("GET", "/attachments/uuid-1", {}) in forge.calls


def test_upload_whose_readback_differs_is_not_reported_as_proof(forge, tmp_path, monkeypatch):
    forge.add(_issue(31, "A"))
    shot = tmp_path / "a.png"
    shot.write_bytes(b"real-bytes")
    original = forge._route

    def corrupt(method, path, query, payload, headers):
        result = original(method, path, query, payload, headers)
        return b"truncated" if path.startswith("/attachments/") else result

    monkeypatch.setattr(forge, "_route", corrupt)
    with pytest.raises(BackendError, match="reads back"):
        backend().attach("#31", str(shot))
    assert forge.comments == []


def test_comment_failure_after_upload_is_partial_success_without_reupload(forge, tmp_path, monkeypatch, capsys):
    forge.add(_issue(32, "A"))
    shot = tmp_path / "a.png"
    shot.write_bytes(b"x")
    b = backend()

    def fail(ticket_id, body):
        raise BackendError("forgejo: HTTP 500 for POST comments")

    monkeypatch.setattr(b, "comment", fail)
    url = b.attach("#32", str(shot))
    assert url == f"{ORIGIN}/attachments/uuid-1" and len(forge.assets) == 1
    assert "not re-uploading" in capsys.readouterr().err


def test_transition_with_a_malformed_patch_response_is_an_error_not_a_stale_ticket(forge, monkeypatch):
    forge.add(_issue(40, "T", labels=forge.label_rows(["status:todo"])))
    original = forge._route

    def empty_patch(method, path, query, payload, headers):
        result = original(method, path, query, payload, headers)
        return None if method == "PATCH" else result

    monkeypatch.setattr(forge, "_route", empty_patch)
    with pytest.raises(BackendError, match="malformed"):
        backend().transition("#40", State.DONE)


def test_label_only_edit_does_not_close_an_open_issue_with_stale_cancelled_label(forge):
    """task-cli#238: a natively-OPEN issue carrying a stale status:cancelled label must not
    be silently re-closed by a metadata-only edit (e.g. `task change #N --label sp:3`)."""
    forge.labels["status:cancelled"] = 99
    forge.add(_issue(41, "Stale", state="open", labels=forge.label_rows(["status:cancelled"])))
    b = backend()
    t = b.get("#41")
    assert t.state == State.CANCELLED  # derived from the stale label
    t.labels.append("sp:3")
    b.update(t)
    patch_payloads = [c[2] for c in forge.calls if c[0] == "PATCH"]
    # the only PATCH should be for labels (via PUT), NOT state
    assert all("state" not in p for p in patch_payloads), (
        "a label-only edit must not send state=closed in the PATCH"
    )
    assert forge.issues[41]["state"] == "open"  # still natively open


def test_transition_still_closes_normally_when_state_label_changes(forge):
    """The #238 fix must not block legitimate state transitions: when the state label itself
    changes (e.g. status:todo → status:done), the native state should be patched."""
    forge.add(_issue(42, "Normal", labels=forge.label_rows(["status:todo"])))
    b = backend()
    t = b.get("#42")
    t.state = State.DONE
    b.update(t)
    patch_payloads = [c[2] for c in forge.calls if c[0] == "PATCH"]
    assert any(p.get("state") == "closed" for p in patch_payloads)


def test_scan_keeps_the_snapshot_of_the_ticket_it_returns(forge, monkeypatch):
    forge.add(_issue(2, "old title"))
    forge.add(_issue(1, "other"))
    original = forge._list
    served = {"n": 0}

    def replay(query):
        rows = original(query)
        served["n"] += 1
        if served["n"] == 2:  # page 2 re-serves #2, edited in between
            return [{**forge.issues[2], "title": "edited elsewhere"}]
        return rows if served["n"] == 1 else []

    monkeypatch.setattr(forge, "_list", replay)
    b = backend()
    (first, *_) = b.list(limit=5)
    assert first.title == "old title"
    assert b._snapshots["2"].title == "old title"


def test_forgejo_body_change_on_ui_spaced_managed_body_is_accepted(forge):
    managed = render(_ticket())
    spaced = "\n".join(line + ("\n" if line.startswith("## ") else "") for line in managed.splitlines()) + "\n\n"
    forge.add(_issue(50, "M", body=spaced))
    b = backend()
    t = b.get("#50")
    t.skips["post-merge-acceptance"] = "verified after deploy"
    b.update(t)
    patch = next(c for c in forge.calls if c[0] == "PATCH")
    assert "## Skipped gates" in patch[2]["body"]

def test_git_origin_config_reader_handles_linked_worktree_commondir(tmp_path):
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "https://git.hyperide.ai/ultrabricks/task-cli.git"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c",
         "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "seed"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-qb", "linked-test", str(wt)],
        check=True,
    )

    assert _git_origin_url_from_config(wt) == (
        "https://git.hyperide.ai/ultrabricks/task-cli.git"
    )
