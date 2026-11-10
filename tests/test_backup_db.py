"""tools/backup_db.sh: a dump counts only once it has been read back (#640).

Drives the real script against a stub `docker` (shebang at byte 0, project
rule) that simulates pg_dump / pg_restore --list / inspect through env knobs.
No real database is involved.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "backup_db.sh"
NAME_GLOB = "idealista-*.dump"

STUB = """#!/bin/bash
case "$1" in
  inspect)
    [ "${STUB_RUNNING:-true}" = true ] && echo true || echo false
    ;;
  exec)
    shift
    case " $* " in
      *" pg_dump "*)
        [ "${STUB_DUMP_FAIL:-0}" = 1 ] && { echo boom >&2; exit 1; }
        [ "${STUB_DUMP_EMPTY:-0}" = 1 ] && exit 0
        [ -n "${STUB_SLEEP:-}" ] && sleep "$STUB_SLEEP"
        printf 'PGDMP-fake-dump'
        ;;
      *" pg_restore "*)
        [ "${STUB_RESTORE_FAIL:-0}" = 1 ] && { echo bad >&2; exit 1; }
        cat >/dev/null
        printf '1; 1 2 TABLE DATA public a x\\n2; 1 3 TABLE DATA public b x\\n'
        ;;
    esac
    ;;
esac
"""


@pytest.fixture()
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(STUB)
    stub.chmod(0o755)
    dest = tmp_path / "daily"
    e = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DOCKER_BIN": str(stub),
        "IDEALISTA_BACKUP_DIR": str(dest),
    }
    return e, dest


def run(e, *args, **extra):
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *args],
        env={**e, **{k: str(v) for k, v in extra.items()}},
        capture_output=True,
        text=True,
        timeout=60,
    )


def dumps(dest):
    return sorted(p.name for p in dest.glob(NAME_GLOB))


def leftovers(dest):
    return [p.name for p in dest.glob(".idealista-tmp.*")]


def test_success_names_file_sets_mode_and_writes_status(env):
    e, dest = env
    r = run(e)
    assert r.returncode == 0, r.stderr
    (name,) = dumps(dest)
    assert len(name) == len("idealista-20260101T000000Z.dump")
    assert name.endswith("Z.dump")
    assert (dest / name).stat().st_mode & 0o777 == 0o600
    status = (dest / "last_success").read_text()
    assert name in status and "bytes" in status and status[:4].isdigit()
    assert not leftovers(dest)


@pytest.mark.parametrize(
    "knob,reason",
    [
        ("STUB_DUMP_FAIL", "pg_dump"),
        ("STUB_DUMP_EMPTY", "empty"),
        ("STUB_RESTORE_FAIL", "pg_restore"),
    ],
)
def test_failed_dump_leaves_no_partial_and_keeps_old_dumps(env, knob, reason):
    e, dest = env
    dest.mkdir()
    old = [f"idealista-2026010{i}T030000Z.dump" for i in range(1, 4)]
    for n in old:
        (dest / n).write_text("old")
    r = run(e, IDEALISTA_BACKUP_KEEP=1, **{knob: 1})
    assert r.returncode != 0
    assert dumps(dest) == old
    assert not leftovers(dest)
    assert reason in (dest / "last_failure").read_text()
    assert not (dest / "last_success").exists()


def test_stopped_container_fails_loudly(env):
    e, dest = env
    r = run(e, STUB_RUNNING="false")
    assert r.returncode != 0
    assert "not running" in r.stderr
    assert dumps(dest) == []
    assert "not running" in (dest / "last_failure").read_text()


def test_rotation_keeps_newest_n_and_spares_foreign_files(env):
    e, dest = env
    dest.mkdir()
    for i in range(1, 6):
        (dest / f"idealista-2026010{i}T030000Z.dump").write_text("old")
    (dest / "pre-merge-abc.dump").write_text("manual")
    (dest / "idealista-notes.dump").write_text("not ours")
    r = run(e, IDEALISTA_BACKUP_KEEP=3)
    assert r.returncode == 0, r.stderr
    kept = dumps(dest)
    ours = [n for n in kept if n != "idealista-notes.dump"]
    assert len(ours) == 3
    assert ours[:2] == [
        "idealista-20260104T030000Z.dump",
        "idealista-20260105T030000Z.dump",
    ]
    assert ours[2] > "idealista-20260105T030000Z.dump"
    assert (dest / "pre-merge-abc.dump").read_text() == "manual"
    assert (dest / "idealista-notes.dump").exists()


def test_concurrent_run_is_refused(env):
    e, dest = env
    slow = subprocess.Popen(
        ["/bin/bash", str(SCRIPT)],
        env={**e, "STUB_SLEEP": "3"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not (dest / ".backup.lock").exists():
            time.sleep(0.05)
        time.sleep(0.5)
        r = run(e)
        assert r.returncode == 75
        assert "refusing to overlap" in r.stderr
    finally:
        slow.wait(timeout=30)
    assert len(dumps(dest)) == 1


def test_verify_prints_table_count_and_rejects_bad_dump(env, tmp_path):
    e, _ = env
    f = tmp_path / "x.dump"
    f.write_text("PGDMP")
    ok = run(e, "--verify", str(f))
    assert ok.returncode == 0 and "2 tables" in ok.stdout
    bad = run(e, "--verify", str(f), STUB_RESTORE_FAIL=1)
    assert bad.returncode == 1
    assert run(e, "--verify", str(tmp_path / "missing")).returncode == 2


def test_script_does_not_read_env_file():
    text = SCRIPT.read_text()
    assert "/.env" not in text and "source .env" not in text
    assert os.access(SCRIPT, os.R_OK)


@pytest.mark.parametrize("keep", ["0", "00", "000"])
def test_a_retention_of_zero_is_refused_before_anything_is_deleted(env, keep):
    e, dest = env
    assert run(e).returncode == 0
    before = dumps(dest)

    result = run(e, IDEALISTA_BACKUP_KEEP=keep)

    assert result.returncode == 2
    assert dumps(dest) == before


def test_a_leading_zero_retention_is_read_as_decimal(env):
    e, dest = env
    for _ in range(2):
        assert run(e, IDEALISTA_BACKUP_KEEP="08").returncode == 0
        time.sleep(1.1)
    assert len(dumps(dest)) == 2


def test_an_interrupted_dump_leaves_no_temp_file(env):
    e, dest = env
    proc = subprocess.Popen(
        ["/bin/bash", str(SCRIPT)],
        env={**e, "STUB_SLEEP": "3"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 10
    while time.time() < deadline and not list(dest.glob(".idealista-tmp.*")):
        time.sleep(0.05)
    assert list(dest.glob(".idealista-tmp.*")), "the dump never started"

    proc.terminate()
    proc.wait(timeout=45)

    assert proc.returncode != 0
    assert not list(dest.glob(".idealista-tmp.*"))
    assert dumps(dest) == []
