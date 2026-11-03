import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "run_import.sh"


@pytest.fixture
def env(tmp_path):
    """Stub PATH with fake `aws` and `neo4j-admin`; fake NEO4J_HOME/SCRATCH."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    fake_s3 = tmp_path / "fake-s3"
    fake_s3.mkdir()
    home = tmp_path / "neo4j-home"
    (home / "data" / "databases").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    data_dir = scratch / "neo4j-data"
    (scratch / "source" / "nodes" / "Person").mkdir(parents=True)
    (scratch / "source" / "relationships" / "WORKS_FOR").mkdir(parents=True)
    (scratch / "source" / "nodes" / "Person" / "person.csv").write_text(
        "id:ID\nPerson-0\n")
    (scratch / "source" / "relationships" / "WORKS_FOR" /
     "works_for.csv").write_text("start:START_ID,end:END_ID\n")

    # fake neo4j-admin: record args + HEAP_SIZE, create fake db + tx dirs, exit 0
    admin = stub_dir / "neo4j-admin"
    admin.write_text(f"""#!/bin/bash
echo "$@" > "{tmp_path / 'admin-args.txt'}"
echo "$HEAP_SIZE" > "{tmp_path / 'admin-heap.txt'}"
mkdir -p "$NEO4J_DATA_DIR/databases/testdb" "$NEO4J_DATA_DIR/transactions/testdb"
echo blob > "$NEO4J_DATA_DIR/databases/testdb/file.db"
echo tx > "$NEO4J_DATA_DIR/transactions/testdb/tx.log"
exit 0
""")
    # fake aws: emulate `aws s3 ls|sync|cp`; size check reads from fake_s3
    aws = stub_dir / "aws"
    aws.write_text(f"""#!/bin/bash
cmd="$1"; sub="$2"; shift 2
case "$cmd" in
  s3)
    case "$sub" in
      ls)
        for arg in "$@"; do
          case "$arg" in
            s3://b/source/)
              echo "2026-11-03 10:00:00        123 source/nodes/Person/person.csv"
              echo "2026-11-03 10:00:00        456 source/relationships/WORKS_FOR/works_for.csv"
              ;;
            s3://b/source/nodes/)
              echo "PRE Person/"
              ;;
            s3://b/source/relationships/)
              echo "PRE WORKS_FOR/"
              ;;
            s3://b/source/nodes/Person/*)
              echo "2026-11-03 10:00:00        123 source/nodes/Person/person.csv"
              exit 0
              ;;
            s3://b/source/relationships/WORKS_FOR/*)
              echo "2026-11-03 10:00:00        456 source/relationships/WORKS_FOR/works_for.csv"
              exit 0
              ;;
            s3://b/databases/*)
              size=$(wc -c < "{fake_s3}/$(basename "$arg")" 2>/dev/null || echo 0)
              echo "2026-11-03 10:00:00 $size $(basename "$arg")"
              exit 0
              ;;
            s3://b/timings/*)
              exit 0
              ;;
          esac
        done
        exit 0
        ;;
      sync) echo "$@" > "{tmp_path / 'sync-args.txt'}"; exit 0 ;;
      cp)
        src="$1"; dest="$2"
        if [[ "$dest" == s3://* ]]; then
          cp "$src" "{fake_s3}/$(basename "$dest")"
          size=$(wc -c < "{fake_s3}/$(basename "$dest")" 2>/dev/null || echo 0)
          echo "$size $dest"
          exit 0
        else
          cp "$src" "$dest"; exit 0
        fi
        ;;
    esac
    ;;
esac
exit 1
""")
    for stub in (admin, aws):
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    e = dict(os.environ)
    e.update(PATH=f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
             NEO4J_HOME=str(home), SCRATCH=str(scratch),
             NEO4J_DATA_DIR=str(data_dir),
             BASH_ENV="", LC_ALL="C")
    return type("Env", (), {
        "env": e, "tmp": tmp_path, "fake_s3": fake_s3, "home": home,
        "scratch": scratch, "data_dir": data_dir})()


def run_script(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env.env,
        capture_output=True, text=True)


def test_dry_run_s3_mode_prints_resolved_command(env):
    r = run_script(env, "--bucket", "b", "--mode", "s3", "--db", "testdb",
                   "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "--nodes=Person=s3://b/source/nodes/Person/person.csv" in r.stdout
    assert ("--relationships=WORKS_FOR="
            "s3://b/source/relationships/WORKS_FOR/works_for.csv") in r.stdout
    assert "database import full testdb" in r.stdout
    # dry-run must not execute the admin tool
    assert not (env.tmp / "admin-args.txt").exists()
    assert not list(env.fake_s3.iterdir())


def test_dry_run_local_mode_uses_scratch_paths(env):
    r = run_script(env, "--bucket", "b", "--mode", "local", "--db", "testdb",
                   "--dry-run")
    assert r.returncode == 0, r.stderr
    assert f"--nodes=Person={env.scratch}/source/nodes/Person/person.csv" in r.stdout


def test_local_mode_imports_and_uploads_artifact(env):
    r = run_script(env, "--bucket", "b", "--mode", "local", "--db", "testdb")
    assert r.returncode == 0, r.stderr
    # neo4j-admin was invoked with local paths
    args = (env.tmp / "admin-args.txt").read_text()
    assert "--verbose" in args
    # artifact uploaded to fake S3 with size verified
    artifacts = list(env.fake_s3.glob("testdb-*.tar.zst"))
    assert len(artifacts) == 1
    # timings JSON uploaded
    assert len(list(env.fake_s3.glob("testdb-*.json"))) == 1


def test_import_behavior_contract(env):
    """Scratch data dir, transactions in artifact, HEAP_SIZE,
    --overwrite-destination, sync --delete."""
    r = run_script(env, "--bucket", "b", "--mode", "local", "--db", "testdb")
    assert r.returncode == 0, r.stderr
    # heap propagated via HEAP_SIZE
    assert (env.tmp / "admin-heap.txt").read_text().strip() == "8g"
    # idempotent re-import
    assert "--overwrite-destination" in (env.tmp / "admin-args.txt").read_text()
    # sync removes stale source files
    assert "--delete" in (env.tmp / "sync-args.txt").read_text()
    # artifact contains both databases/ and transactions/ trees
    artifact = next(env.fake_s3.glob("testdb-*.tar.zst"))
    listing = subprocess.run(
        ["bash", "-c", f"zstd -dc '{artifact}' | tar -t"],
        capture_output=True, text=True, check=True).stdout
    names = listing.split()
    assert "databases/testdb/file.db" in names, names
    assert "transactions/testdb/tx.log" in names, names
    # source scratch removed after successful local import
    assert not (env.scratch / "source").exists()


def test_missing_source_fails_before_admin_invocation(env):
    # empty source: remove generated files
    for f in env.scratch.rglob("*.csv"):
        f.unlink()
    # fake aws reports no objects for source listing
    aws_stub = Path(env.env["PATH"].split(":")[0]) / "aws"
    aws_stub.write_text("""#!/bin/bash
for arg in "$@"; do
  case "$arg" in
    s3://b/source/|s3://b/source/nodes/) exit 0 ;;
    s3://b/source/relationships/) exit 0 ;;
  esac
done
exit 0
""")
    aws_stub.chmod(aws_stub.stat().st_mode | stat.S_IEXEC)
    r = run_script(env, "--bucket", "b", "--mode", "s3", "--dry-run")
    assert r.returncode != 0
    assert "no source" in (r.stdout + r.stderr).lower()
    assert not (env.tmp / "admin-args.txt").exists()


def test_upload_size_mismatch_fails_run(env):
    # fake aws that reports a wrong remote size after upload
    aws_stub = Path(env.env["PATH"].split(":")[0]) / "aws"
    original = aws_stub.read_text()
    # corrupt the size reported by `aws s3 ls s3://b/databases/<artifact>`
    good = f'size=$(wc -c < "{env.fake_s3}/$(basename "$arg")" 2>/dev/null || echo 0)'
    assert good in original, "fixture stub no longer contains expected size line"
    aws_stub.write_text(original.replace(good, 'size=999999'))
    aws_stub.chmod(aws_stub.stat().st_mode | stat.S_IEXEC)
    r = run_script(env, "--bucket", "b", "--mode", "local", "--db", "testdb")
    assert r.returncode != 0
    assert "size mismatch" in (r.stdout + r.stderr).lower()
