from pathlib import Path

from lib.user_auth import UserAuthStore


def test_users_sessions_states_and_project_scope(tmp_path: Path):
    store = UserAuthStore(tmp_path / "users.sqlite3", tmp_path / "projects")
    state = store.create_oauth_state("wechat", "/web/")
    assert store.consume_oauth_state(state, "wechat") == "/web/"
    assert store.consume_oauth_state(state, "wechat") is None

    alice = store.upsert_user("wechat", "openid-a", "Alice")
    bob = store.upsert_user("wechat", "openid-b", "Bob")
    token, _ = store.create_session(alice["id"])
    assert store.user_for_session(token)["id"] == alice["id"]

    alice_project = store.project_path(alice["id"], "demo")
    alice_project.mkdir(parents=True)
    assert alice_project.parent == store.user_projects_root(alice["id"])
    assert list(store.user_projects_root(bob["id"]).iterdir()) == []


def test_project_ids_cannot_escape_user_root(tmp_path: Path):
    store = UserAuthStore(tmp_path / "users.sqlite3", tmp_path / "projects")
    user = store.upsert_user("wechat", "openid-a")
    project = store.project_path(user["id"], "../outside")
    assert project.parent == store.user_projects_root(user["id"])


def test_asset_upload_is_scoped_to_owner(tmp_path: Path):
    store = UserAuthStore(tmp_path / "users.sqlite3", tmp_path / "projects")
    alice = store.upsert_user("wechat", "openid-a")
    bob = store.upsert_user("wechat", "openid-b")
    asset = store.save_asset(alice["id"], "demo", "photo.png", "aGVsbG8=")
    assert asset["path"].startswith(str(store.user_projects_root(alice["id"])))
    assert not list(store.user_projects_root(bob["id"]).rglob("photo.png"))
