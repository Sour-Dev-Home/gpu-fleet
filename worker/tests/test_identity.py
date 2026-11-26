"""The worker's install ID (Q87, ADR 0026): random, made once, kept per user, never in the repo."""

import os
import uuid
from pathlib import Path

import pytest
from fleet_worker import identity


def env_for(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Every place the ID could be stored, pointed into the test's temp dir."""
    return {
        "APPDATA": str(tmp_path / "appdata"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        **extra,
    }


def test_the_first_start_makes_a_random_uuid_and_keeps_it(tmp_path):
    env = env_for(tmp_path)
    first = identity.install_id(env, windows=False)
    assert uuid.UUID(first).version == 4
    assert identity.install_id(env, windows=False) == first
    assert identity.install_id(env, windows=False) == first  # and again: no rewrite, no drift
    assert (tmp_path / "xdg" / "fleet" / "worker-id").read_text(encoding="utf-8").strip() == first


def test_two_installs_get_different_ids(tmp_path):
    a = identity.install_id(env_for(tmp_path / "a"), windows=False)
    b = identity.install_id(env_for(tmp_path / "b"), windows=False)
    assert a != b


def test_windows_stores_it_under_appdata(tmp_path):
    env = env_for(tmp_path)
    value = identity.install_id(env, windows=True)
    assert (tmp_path / "appdata" / "fleet" / "worker-id").read_text(
        encoding="utf-8"
    ).strip() == value
    assert not (tmp_path / "xdg").exists()


def test_elsewhere_it_defaults_to_dot_config_under_home(tmp_path):
    env = env_for(tmp_path)
    del env["XDG_CONFIG_HOME"]
    value = identity.install_id(env, windows=False)
    assert (tmp_path / "home" / ".config" / "fleet" / "worker-id").read_text(
        encoding="utf-8"
    ).strip() == value


def test_the_env_override_wins_and_writes_nothing(tmp_path):
    env = env_for(tmp_path, FLEET_WORKER_ID="notebook-7")
    assert identity.install_id(env, windows=False) == "notebook-7"
    assert not (tmp_path / "xdg").exists()
    # and it does not replace a stored ID: unsetting it brings the install's own back
    own = identity.install_id(env_for(tmp_path), windows=False)
    assert identity.install_id(env, windows=False) == "notebook-7"
    assert identity.install_id(env_for(tmp_path), windows=False) == own


@pytest.mark.parametrize("bad", ["has space", "x" * 65, "a/b", "new\nline", "café"])
def test_an_odd_override_is_refused(tmp_path, bad):
    with pytest.raises(ValueError, match="FLEET_WORKER_ID"):
        identity.install_id(env_for(tmp_path, FLEET_WORKER_ID=bad), windows=False)


def test_a_blank_override_means_not_set(tmp_path):
    value = identity.install_id(env_for(tmp_path, FLEET_WORKER_ID="  "), windows=False)
    assert uuid.UUID(value)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits; Windows relies on the profile ACL"
)
def test_the_file_and_folder_are_user_only(tmp_path):
    identity.install_id(env_for(tmp_path), windows=False)
    folder = tmp_path / "xdg" / "fleet"
    assert (folder / "worker-id").stat().st_mode & 0o777 == 0o600
    assert folder.stat().st_mode & 0o777 == 0o700
    assert list(folder.iterdir()) == [folder / "worker-id"]  # no temp file left behind


def test_the_file_is_requested_user_only_on_every_os(tmp_path, monkeypatch):
    # The permission bits only show on POSIX; what the code asks the OS for shows everywhere.
    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, **kw):
        modes.append(mode)
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", spy)
    identity.install_id(env_for(tmp_path), windows=False)
    assert modes == [0o600]


def test_the_temp_file_is_created_exclusively_and_never_through_a_symlink(tmp_path, monkeypatch):
    flags = []
    real_open = os.open

    def spy(path, f, mode=0o777, **kw):
        flags.append(f)
        return real_open(path, f, mode, **kw)

    monkeypatch.setattr(os, "open", spy)
    identity.install_id(env_for(tmp_path), windows=False)
    (f,) = flags
    assert f & os.O_CREAT and f & os.O_EXCL and f & os.O_WRONLY
    assert not f & os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        assert f & os.O_NOFOLLOW


def test_a_planted_temp_name_is_replaced_and_what_it_pointed_at_is_untouched(tmp_path):
    folder = tmp_path / "xdg" / "fleet"
    folder.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    planted = folder / f"worker-id.{os.getpid()}.tmp"
    try:
        planted.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need a privilege here")
    value = identity.install_id(env_for(tmp_path), windows=False)
    assert victim.read_text(encoding="utf-8") == "precious"
    assert (folder / "worker-id").read_text(encoding="utf-8").strip() == value
    assert list(folder.iterdir()) == [folder / "worker-id"]


def test_a_left_over_temp_file_is_removed_and_the_write_retried(tmp_path):
    folder = tmp_path / "xdg" / "fleet"
    folder.mkdir(parents=True)
    (folder / f"worker-id.{os.getpid()}.tmp").write_text("stale", encoding="utf-8")
    value = identity.install_id(env_for(tmp_path), windows=False)
    assert (folder / "worker-id").read_text(encoding="utf-8").strip() == value
    assert list(folder.iterdir()) == [folder / "worker-id"]


def test_a_damaged_file_is_replaced_not_trusted(tmp_path):
    env = env_for(tmp_path)
    path = tmp_path / "xdg" / "fleet" / "worker-id"
    path.parent.mkdir(parents=True)
    path.write_text("not a uuid\n", encoding="utf-8")
    value = identity.install_id(env, windows=False)
    assert uuid.UUID(value)
    assert identity.install_id(env, windows=False) == value
    assert path.read_text(encoding="utf-8").strip() == value


def test_a_folder_that_cannot_be_written_gives_an_id_for_this_run_only(tmp_path, caplog):
    blocker = tmp_path / "xdg"
    blocker.write_text("a file where the folder should go", encoding="utf-8")
    value = identity.install_id(env_for(tmp_path), windows=False)  # must not crash the worker
    assert uuid.UUID(value)
    assert "not saved" in caplog.text
    assert str(tmp_path) not in caplog.text  # no local paths in log lines
