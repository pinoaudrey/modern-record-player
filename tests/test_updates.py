import subprocess
from pathlib import Path

import pytest

from vinyl import updates
from vinyl.updates import UpdateError, apply, check, restart_command, version

REPO = Path("/home/pi/Documents/modern-record-player")


class FakeRun:
    """Stands in for subprocess.run: answers by matching the start of argv."""

    def __init__(self, **answers):
        # key: space-joined argv prefix -> (returncode, stdout) or an Exception
        self.answers = {k.replace("_", " "): v for k, v in answers.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for prefix, answer in sorted(self.answers.items(), key=lambda kv: -len(kv[0])):
            if joined.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                rc, out = answer
                return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="" if rc == 0 else out)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def ran(self, *prefix) -> bool:
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


@pytest.fixture
def run(monkeypatch):
    def install(**answers):
        fake = FakeRun(**answers)
        monkeypatch.setattr(updates.subprocess, "run", fake)
        return fake
    return install


# --- check --------------------------------------------------------------------

def test_check_reports_commits_behind(run):
    fake = run(**{
        "git rev-list": (0, "2\n"),
        "git log": (0, "Fix the platter cooldown\n"),
    })
    assert check(REPO) == (2, "Fix the platter cooldown")
    assert fake.calls[0][:3] == ["git", "fetch", "--quiet"]


def test_check_up_to_date_skips_log(run):
    fake = run(**{"git rev-list": (0, "0\n")})
    assert check(REPO) == (0, None)
    assert not fake.ran("git", "log")


def test_check_fetch_failure_is_an_update_error(run):
    run(**{"git fetch": (128, "fatal: could not read from remote repository")})
    with pytest.raises(UpdateError, match="could not read"):
        check(REPO)


def test_check_without_git_is_an_update_error(run):
    run(**{"git": FileNotFoundError("git")})
    with pytest.raises(UpdateError, match="git not found"):
        check(REPO)


# --- apply --------------------------------------------------------------------

def _pull_scenario(run, changed="vinyl/player.py\n", restart_rc=0):
    heads = iter(["aaa1111\n", "bbb2222\n"])

    def rev_parse(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=next(heads), stderr="")

    fake = run(**{
        "git diff": (0, changed),
        "sudo -n /usr/bin/systemctl restart": (restart_rc, "" if restart_rc == 0 else "sudo: a password is required"),
    })
    fake.answers["git rev-parse"] = None  # handled below
    original = fake.__call__

    def call(argv, **kw):
        if argv[:2] == ["git", "rev-parse"]:
            fake.calls.append(list(argv))
            return rev_parse(argv)
        return original(argv, **kw)

    fake.__call__ = call  # not used by subprocess.run monkeypatch; patch the instance call path instead
    return fake, call


@pytest.fixture
def pull(monkeypatch, run):
    def make(changed="vinyl/player.py\n", restart_rc=0):
        fake, call = _pull_scenario(run, changed, restart_rc)
        monkeypatch.setattr(updates.subprocess, "run", call)
        return fake
    return make


def test_apply_pulls_installs_and_restarts(pull):
    fake = pull(changed="pyproject.toml\nvinyl/web.py\n")
    result = apply(REPO, user="pi")
    assert (result.before, result.after, result.updated) == ("aaa1111", "bbb2222", True)
    assert fake.ran("git", "pull", "--ff-only")
    assert fake.ran(str(REPO / ".venv/bin/pip"), "install", "-q", "-e", ".")
    assert result.deps_installed is True
    assert fake.ran("sudo", "-n", "/usr/bin/systemctl", "restart", "record-player@pi")
    assert result.restarted is True and result.warning is None
    assert "Dependencies reinstalled" in result.summary


def test_apply_skips_pip_when_pyproject_unchanged(pull):
    fake = pull(changed="vinyl/player.py\nREADME.md\n")
    result = apply(REPO, user="pi")
    assert result.updated and not result.deps_installed
    assert not any("pip" in c[0] for c in fake.calls)
    assert result.restarted


def test_apply_failed_restart_is_a_warning_not_an_error(pull):
    fake = pull(restart_rc=1)
    result = apply(REPO, user="pi")
    assert result.updated is True
    assert result.restarted is False
    assert "password is required" in result.warning and "manually" in result.warning
    assert fake.ran("git", "pull", "--ff-only")


def test_apply_can_leave_restart_to_the_caller(pull):
    fake = pull()
    result = apply(REPO, restart=False, user="pi")
    assert result.updated and not result.restarted and result.warning is None
    assert not fake.ran("sudo")


def test_apply_when_already_current_does_nothing_else(run):
    fake = run(**{"git rev-parse": (0, "aaa1111\n")})
    result = apply(REPO, user="pi")
    assert result.updated is False and result.before == result.after == "aaa1111"
    assert not fake.ran("sudo") and not fake.ran("git", "diff")
    assert result.summary.startswith("Already up to date")


def test_apply_pull_failure_raises(run):
    run(**{"git rev-parse": (0, "aaa1111\n"), "git pull": (1, "fatal: Not possible to fast-forward, aborting.")})
    with pytest.raises(UpdateError, match="fast-forward"):
        apply(REPO, user="pi")


# --- helpers ------------------------------------------------------------------

def test_restart_command_matches_the_sudoers_rule():
    assert restart_command("pi") == ["sudo", "-n", "/usr/bin/systemctl", "restart", "record-player@pi"]


def test_can_sudo_asks_sudo_without_running(run):
    fake = run(**{"sudo -n -l": (0, "/usr/bin/systemctl restart record-player@pi\n")})
    assert updates.can_sudo(restart_command("pi")) is True
    assert fake.calls == [["sudo", "-n", "-l", "/usr/bin/systemctl", "restart", "record-player@pi"]]
    run(**{"sudo -n -l": (1, "Sorry, user pi may not run sudo")})
    assert updates.can_sudo(restart_command("pi")) is False
    assert updates.can_sudo(["systemctl", "restart", "x"]) is False


def test_version_from_git_log(run):
    run(**{"git log": (0, "eec3a15 (2026-09-09)\n")})
    assert version(REPO) == "eec3a15 (2026-09-09)"


def test_version_unknown_without_git(run):
    run(**{"git": FileNotFoundError("git")})
    assert version(REPO) == "unknown"
    run(**{"git log": (128, "fatal: not a git repository")})
    assert version(REPO) == "unknown"
