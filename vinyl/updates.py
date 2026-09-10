"""Self-update from git.

check()  fetches and reports how far HEAD is behind its upstream.
apply()  fast-forwards, reinstalls the package if pyproject.toml changed, and
         restarts the systemd service through the sudo rule that
         deploy/install-pi.sh installs (/etc/sudoers.d/record-player).

Used by the admin's status page ("Update now") and by the nightly
record-player-update@<user>.timer, which runs `python -m vinyl update`.
"""

import getpass
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SERVICE = "record-player@{user}"
SYSTEMCTL = "/usr/bin/systemctl"
REBOOT = "/usr/sbin/reboot"
GIT_TIMEOUT = 120
PIP_TIMEOUT = 600


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateResult:
    before: str
    after: str
    updated: bool                 # HEAD moved
    deps_installed: bool = False  # pyproject.toml changed, pip ran
    restarted: bool = False
    warning: str | None = None    # something that needs a human (restart failed, ...)
    log: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.updated:
            return f"Already up to date ({self.after})."
        s = f"Updated {self.before} -> {self.after}."
        if self.deps_installed:
            s += " Dependencies reinstalled."
        if self.restarted:
            s += " Service restarted."
        return s


def _run(argv: list[str], cwd: Path | None = None, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as e:
        raise UpdateError(f"{argv[0]} not found") from e
    except subprocess.TimeoutExpired as e:
        raise UpdateError(f"{' '.join(argv[:2])} timed out") from e


def _git(repo: Path, *args: str) -> str:
    r = _run(["git", *args], cwd=repo)
    if r.returncode != 0:
        raise UpdateError((r.stderr or r.stdout or f"git {args[0]} failed").strip())
    return r.stdout.strip()


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "pi"


def version(repo: Path) -> str:
    """'eec3a15 (2026-09-09)' from `git log -1`, or 'unknown' without git."""
    try:
        r = _run(["git", "log", "-1", "--format=%h (%cs)"], cwd=repo, timeout=10)
    except UpdateError:
        return "unknown"
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"


def check(repo: Path) -> tuple[int, str | None]:
    """Fetch, then (commits HEAD is behind its upstream, subject of the newest one)."""
    _git(repo, "fetch", "--quiet")
    behind = int(_git(repo, "rev-list", "--count", "HEAD..@{u}") or 0)
    latest = _git(repo, "log", "-1", "--format=%s", "@{u}") if behind else None
    return behind, latest


def restart_command(user: str | None = None) -> list[str]:
    return ["sudo", "-n", SYSTEMCTL, "restart", SERVICE.format(user=user or current_user())]


def reboot_command() -> list[str]:
    return ["sudo", "-n", REBOOT]


def can_sudo(argv: list[str]) -> bool:
    """True if `sudo -n argv...` is allowed without a password (sudo -l does not run it)."""
    if argv[:2] != ["sudo", "-n"]:
        return False
    try:
        r = _run(["sudo", "-n", "-l", *argv[2:]], timeout=10)
    except UpdateError:
        return False
    return r.returncode == 0


def restart_service(user: str | None = None) -> str | None:
    """Restart the player's unit. Returns a warning message on failure, None on success."""
    argv = restart_command(user)
    try:
        r = _run(argv, timeout=60)
    except UpdateError as e:
        return f"Restart failed ({e}); the code is updated, restart the service manually."
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip() or f"exit {r.returncode}"
        return (
            f"Restart failed ({detail}); the code is updated, restart the service manually: "
            f"sudo systemctl restart {SERVICE.format(user=user or current_user())}"
        )
    return None


def apply(repo: Path, restart: bool = True, user: str | None = None) -> UpdateResult:
    """git pull --ff-only; pip install if pyproject.toml changed; restart the service."""
    repo = Path(repo)
    before = _git(repo, "rev-parse", "--short", "HEAD")
    pulled = _git(repo, "pull", "--ff-only", "--quiet")
    after = _git(repo, "rev-parse", "--short", "HEAD")
    result = UpdateResult(before=before, after=after, updated=before != after)
    if pulled:
        result.log.append(pulled)
    if not result.updated:
        return result
    log.info("Updated %s -> %s", before, after)

    changed = _git(repo, "diff", "--name-only", before, after).splitlines()
    if "pyproject.toml" in changed:
        pip = repo / ".venv" / "bin" / "pip"
        r = _run([str(pip), "install", "-q", "-e", "."], cwd=repo, timeout=PIP_TIMEOUT)
        if r.returncode != 0:
            raise UpdateError(f"pip install failed: {(r.stderr or r.stdout).strip()}")
        result.deps_installed = True
        log.info("pyproject.toml changed, dependencies reinstalled")

    if restart:
        result.warning = restart_service(user)
        result.restarted = result.warning is None
        if result.warning:
            log.warning(result.warning)
    return result
