"""Integration self-check tests for the ``capture`` axis (US1).

These tests validate the network-capture feature (see
``specs/002-network-capture/``) from inside a capture-enabled session. They
are written to run **only** when ``--zk-features=capture`` is active and are
skipped otherwise by the existing ``zk_features`` marker machinery
(``kazoo.testing.kazoo_ensemble``), so the same file is safe in every matrix
cell.

* ``test_capture_feature_active`` -- the axis wiring (R-04): the capture
  overlay is layered onto the compose file list.
* ``test_artifact_exists_and_valid`` -- the artifact contract (FR-002/FR-003):
  every member's tshark sidecar holds open a ``kazoo-client-zooN-*.pcapng``
  that already carries real client-port frames and a structurally valid pcapng
  header.

Per the network-holder split (docker-compose.base.yml), capture is now **per
member**: one ``zooN-capture`` sidecar joins each member's network namespace
and writes a uniquely-named per-run file (``kazoo-client-zooN-<ts>.pcapng``,
see docker-compose.features-capture.yml). The artifact is therefore a
*collection* — the test verifies each member has produced a file, and that the
union of frames across the members carries client-port traffic (the Kazoo
client connects to whichever ensemble member it happens to pick, so frames may
land on any single member).

Two views of the artifact are exercised, because the "when is it readable"
answer differs between Docker Desktop and native Linux:

* **Container-side (authoritative, live):** ``docker compose exec`` into each
  capture container. The file is open there for the whole session, its Section
  Header Block is written as soon as capture starts, and captured frames are
  flushed into it continuously. This works identically on every platform, so
  it is the required gate.
* **Host-side (best effort):** on native Linux the bind mount mirrors the
  live file immediately, mirroring the quickstart V1 end-of-session view. On
  Docker Desktop, virtiofs only syncs the host's view of an open,
  actively-written file back once the writing process exits, so the host
  mount is expected to be empty or absent *while the session is running*;
  the authoritative post-session ``capinfos`` gate there is the manual V1
  check. The host probe therefore hard-fails only when a file is present
  but malformed, and tolerates the stale/absent Docker Desktop view.

Per the plan, the host needs no capture tooling (FR-009); ``capinfos`` is an
optional extra exercised only when it happens to be installed and the host
view is readable.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

# pcapng magic bytes: 0A 0D 0D 0A (native endian) identifies the Section
# Header Block (SHB); the byte-swapped variant is produced by non-native
# writers, so accept either.
_PCAPNG_MAGIC = (b"\x0a\x0d\x0d\x0a", b"\x4d\x3c\xb2\xa1")

_CAPTURE_OVERLAY = "docker-compose.features-capture.yml"

#: Ensemble members and their capture sidecar services. Each member's capture
#: writes its own uniquely-named pcapng (see capture-entrypoint.sh).
_MEMBERS = ("zoo1", "zoo2", "zoo3")
_CAPTURE_SERVICES = tuple(f"{m}-capture" for m in _MEMBERS)


@pytest.mark.zk_features(require=["capture"])
def test_capture_feature_active(docker_compose_config):
    """A capture-enabled run must not be skipped and must layer the overlay."""
    # Running at all proves the ``zk_features(require=["capture"])`` marker did
    # not skip this item (R-04/marker machinery).
    assert _CAPTURE_OVERLAY in docker_compose_config["compose_files"]


@pytest.mark.zk_features(require=["capture"])
def test_artifact_exists_and_valid(docker_compose, zkclient):
    """Every member's capture artifact exists and is a valid pcapng."""
    # Requesting the session-scoped docker_compose fixture guarantees the
    # stack (including the per-member capture sidecars) has been
    # ``up --wait``-ed before this assertion runs. The zkclient fixture
    # additionally drives real client traffic across the compose bridge (clear
    # port 2181), which the tshark sidecars are capturing; without it the
    # artifacts would stay empty for this standalone self-check.
    zkclient.create("/capture-selfcheck", b"x")
    assert zkclient.get("/capture-selfcheck")[0] == b"x"

    # Container-side gate: each sidecar's pcapng is open, its Section Header
    # Block is on disk, and captured client frames are already being flushed
    # into it. This is live on every platform (see module docstring).
    magics = {
        member: _wait_for_container_pcapng_magic(docker_compose, member)
        for member in _MEMBERS
    }
    _assert_container_frames_present(docker_compose)

    # Host-side probe: assert existence+validity when the bind mount actually
    # mirrors the live files (native Linux); tolerate Docker Desktop's stale
    # virtiofs view mid-session (validated post-session by quickstart V1).
    artifacts = list(
        Path(os.environ["ZK_WORK_DIR"]).glob("captures/kazoo-client-*.pcapng")
    )
    _probe_host_artifacts(artifacts, magics)


def _container_exec(
    docker_compose, member: str, command: list[str]
) -> tuple[str, str, int]:
    """Run a command inside the ``{member}-capture`` sidecar container."""
    stdout, stderr, exit_code = docker_compose.exec_in_container(
        command, service_name=f"{member}-capture"
    )
    return stdout, stderr, exit_code


def _member_pcapngs(member: str) -> str:
    """Shell glob matching all capture files written by a member's sidecar.

    One file per sidecar invocation (per run/session); the sidecar may have
    been recreated, so match all of them and read the newest.
    """
    return f"/captures/kazoo-client-{member}-*.pcapng"


def _newest_member_pcapng(docker_compose, member: str) -> str:
    """The newest capture file a member's sidecar has written (path in the
    container)."""
    stdout, _stderr, exit_code = _container_exec(
        docker_compose,
        member,
        ["sh", "-c", f"ls -t {_member_pcapngs(member)} 2>/dev/null | head -1"],
    )
    if exit_code != 0 or not stdout.strip():
        raise AssertionError(
            f"no capture file for {member} "
            f"(service {member}-capture, glob {_member_pcapngs(member)})"
        )
    return stdout.strip().splitlines()[0]


def _wait_for_container_pcapng_magic(docker_compose, member: str) -> bytes:
    """Poll a member's sidecar for a readable pcapng Section Header Block.

    Each capture service writes its pcapng Section Header Block to its output
    file as soon as capture starts, so polling the container's own view
    converges in at most a couple of flush cycles and is immune to any
    host-side mount staleness.
    """
    last_magic = b""
    last_error: Exception | None = None
    for _ in range(50):
        try:
            newest = _newest_member_pcapng(docker_compose, member)
            stdout, _stderr, exit_code = _container_exec(
                docker_compose,
                member,
                ["sh", "-c", f"head -c 4 {newest}"],
            )
            if exit_code == 0:
                last_magic = stdout.encode("latin-1")
            if last_magic:
                break
        except (RuntimeError, OSError) as exc:
            last_error = exc
        time.sleep(0.2)
    if not last_magic:
        raise AssertionError(
            f"capture sidecar {member}-capture pcapng missing or unreadable"
            + (f" ({last_error})" if last_error else "")
        )
    # A pcapng SHB leads the file; legacy pcap (D4 C3 B2 A1) is not expected
    # (tshark writes pcapng natively, FR-004).
    assert (
        last_magic in _PCAPNG_MAGIC
    ), f"{member}: not a pcapng header ({last_magic!r})"
    return last_magic


def _assert_container_frames_present(docker_compose) -> None:
    """The sidecars must have captured at least one client-port frame.

    tshark writes captured packet data to the pcapng in flushes, so like the
    magic gate this polls (up to ~10s) until frames become readable rather
    than asserting on the first read (R-05: clean in-band flush during the
    session, plus the final flush at teardown). The Kazoo client connects to
    a single ensemble member, so frames may appear on any one capture; the
    union across all members must include a client port.
    """
    ports: set[int] = set()
    for _ in range(50):
        for member in _MEMBERS:
            try:
                newest = _newest_member_pcapng(docker_compose, member)
            except AssertionError:
                continue
            stdout, _stderr, exit_code = _container_exec(
                docker_compose,
                member,
                [
                    "tshark",
                    "-r",
                    newest,
                    "-T",
                    "fields",
                    "-e",
                    "tcp.port",
                ],
            )
            if exit_code == 0 and stdout.strip():
                # `-T fields -e tcp.port` emits one "sport,dport" pair per
                # frame; split on both whitespace and comma.
                tokens = [
                    tok for line in stdout.split() for tok in line.split(",")
                ]
                ports |= {int(tok) for tok in tokens if tok.isdigit()}
        if ports & {2181, 2281}:
            break
        time.sleep(0.2)
    assert ports & {2181, 2281}, (
        f"no client-port frames captured across {_MEMBERS} (ports="
        f"{sorted(ports)})"
    )


def _probe_host_artifacts(
    artifacts: list[Path], magics: dict[str, bytes]
) -> None:
    """Best-effort host-side artifact probe (see module docstring).

    Hard-fails only when a host file exists but disagrees with the
    container-side artifact (i.e. a genuinely inconsistent bind mount).
    Skips silently when the host cannot reflect the live file yet — the
    quickstart V1 post-session capinfos gate covers that case.
    """
    if not artifacts:
        return  # not yet visible on the host (expected on Docker Desktop)
    for artifact in artifacts:
        try:
            with artifact.open("rb") as handle:
                head = handle.read(4)
        except OSError:
            continue  # racing the mount; another probe will cover it
        # The host may expose multiple members' files; any valid pcapng magic
        # is acceptable (each sidecar writes its own SHB).
        assert head in _PCAPNG_MAGIC, (
            f"host artifact {artifact.name} out of sync with sidecar: "
            f"{head!r}"
        )
    _run_capinfos_if_available(artifacts)


def _run_capinfos_if_available(artifacts: list[Path]) -> None:
    """Validate with ``capinfos`` when the optional host tool is present.

    The plan guarantees the host needs no capture tooling (FR-009); the
    quickstart V1 manual check is the authoritative capinfos gate.
    When capinfos *is* installed we still exercise it here for automated
    coverage, tolerating the transient "in progress" state of a live capture.
    """
    capinfos = shutil.which("capinfos")
    if capinfos is None:
        return
    for artifact in artifacts:
        result = subprocess.run(
            [capinfos, str(artifact)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # ``returncode != 0`` would mean the tool could not parse the file; a
        # mid-capture file is expected to remain readable (R-05).
        assert (
            result.returncode == 0
        ), f"capinfos failed on {artifact}:\n{result.stdout}\n{result.stderr}"
