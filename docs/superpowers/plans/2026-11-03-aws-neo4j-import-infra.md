# AWS neo4j-admin S3 Import Infra — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Terraform-provisioned EC2 + S3 in eu-west-1 running `neo4j-admin database import` from synthetic CSV/Parquet data, with switchable S3-direct vs local-NVMe import modes and mandatory artifact upload back to S3.

**Architecture:** Single Terraform root module provisions S3 bucket, m7gd.4xlarge EC2 (instance profile with S3-only IAM), and user_data installs Neo4j 2026.10.1 Enterprise tarball + RAID0 NVMe scratch. A Python generator writes admin-import-compatible CSV/Parquet to S3. A bash runner wraps `neo4j-admin database import full`, supports both modes, uploads the resulting database tarball, and logs timings.

**Tech Stack:** Python 3.14 (boto3, pyarrow, pytest, moto), Bash, Terraform (>= 1.6, AWS provider >= 5.0), Neo4j 2026.10.1 Enterprise.

**Spec:** `docs/superpowers/specs/2026-11-03-aws-neo4j-import-infra-design.md`

## Global Constraints

- Region: `eu-west-1`. Instance type: `m7gd.4xlarge`. Neo4j: `2026.10.1` Enterprise tarball (pinned via Terraform variable).
- Python `requires-python = ">=3.14"` (from pyproject.toml). Runtime deps: boto3, pyarrow. Dev deps: pytest, moto.
- Bucket prefixes: `source/` (generated data), `databases/` (artifacts), `timings/` (run timings).
- Artifact upload is mandatory: `<db>-<timestamp>.tar.zst` under `databases/`, size-verified; upload failure = runner failure.
- IAM instance profile: read/write on the one bucket only.
- S3 bucket `force_destroy = true` — bucket holds only generated test data; `terraform destroy` deletes objects.
- Generated files never appear half-written: writers stage locally, S3 uploads happen after complete local write.
- Conventional commit messages (`feat:`, `test:`, `chore:`, `docs:`).

## Review Focus

Inputs/failure modes the spec implies but task tests must pin:

1. **Chunking boundary**: `--rows-per-file 0` (one file per label) and chunked mode must both write every row exactly once — no dropped/duplicated boundary chunk. Tests in Task 2.
2. **Unknown property type in config**: e.g. `"type": "uuid"` must fail with a clear error naming the property, not silently produce empty columns. Test in Task 1.
3. **Seed determinism**: same `--seed` twice produces byte-identical output rows. Test in Task 1.
4. **Empty or missing source data**: runner with no generated files must fail with a clear message BEFORE invoking `neo4j-admin`. Test in Task 4.
5. **Corrupted artifact upload**: remote object size differs from local archive size — runner must exit non-zero. Test in Task 4.

---

### Task 1: Generator core — config loading and row generation

**Files:**
- Create: `generator/__init__.py` (empty)
- Create: `generator/generate.py`
- Create: `generator/tests/__init__.py` (empty)
- Create: `generator/tests/test_generate.py`
- Modify: `pyproject.toml` (dev deps, pytest config)

**Interfaces:**
- Consumes: nothing (first task).
- Produces (imported by Tasks 2 and 3):
  - `generator/generate.py`:
    - `@dataclass(frozen=True) class Property(name: str, type: str)` — `type` is one of `string|int|float|bool|timestamp`
    - `@dataclass class NodeSpec(label: str, count: int, properties: list[Property])`
    - `@dataclass class RelSpec(type: str, count: int, start: str, end: str, properties: list[Property])`
    - `load_config(path: str) -> tuple[list[NodeSpec], list[RelSpec]]`
    - `generate_node_rows(spec: NodeSpec, seed: int) -> list[dict]`
    - `generate_rel_rows(spec: RelSpec, node_counts: dict[str, int], seed: int) -> list[dict]`
    - `PROPERTY_TYPES = ("string", "int", "float", "bool", "timestamp")`

- [ ] **Step 1: Update pyproject.toml with dev deps and pytest config**

```toml
[project]
name = "neo4j-admin-s3-import-tests"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["boto3>=1.35", "pyarrow>=17"]

[dependency-groups]
dev = ["pytest>=8", "moto>=5"]

[tool.pytest.ini_options]
testpaths = ["generator/tests", "scripts/tests"]
```

- [ ] **Step 2: Write failing tests**

`generator/tests/test_generate.py`:

```python
import json
from pathlib import Path

import pytest

from generator.generate import (
    Property, NodeSpec, RelSpec, load_config,
    generate_node_rows, generate_rel_rows,
)

CONFIG = {
    "nodes": [
        {"label": "Person", "count": 100,
         "properties": [{"name": "name", "type": "string"},
                        {"name": "age", "type": "int"}]},
        {"label": "Company", "count": 10,
         "properties": [{"name": "name", "type": "string"}]},
    ],
    "relationships": [
        {"type": "WORKS_FOR", "count": 90, "start": "Person", "end": "Company",
         "properties": [{"name": "since", "type": "int"}]},
    ],
}

@pytest.fixture
def config_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(CONFIG))
    return str(p)

def test_load_config_roundtrip(config_file):
    nodes, rels = load_config(config_file)
    assert nodes == [
        NodeSpec(label="Person", count=100,
                 properties=[Property("name", "string"), Property("age", "int")]),
        NodeSpec(label="Company", count=10, properties=[Property("name", "string")]),
    ]
    assert rels == [
        RelSpec(type="WORKS_FOR", count=90, start="Person", end="Company",
                properties=[Property("since", "int")]),
    ]

def test_load_config_rejects_unknown_property_type(tmp_path):
    bad = json.loads(json.dumps(CONFIG))
    bad["nodes"][0]["properties"][0]["type"] = "uuid"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="Person.name.*uuid"):
        load_config(str(p))

def test_node_rows_count_ids_and_properties():
    spec = NodeSpec("Person", 100,
                    [Property("name", "string"), Property("age", "int")])
    rows = generate_node_rows(spec, seed=42)
    assert len(rows) == 100
    assert rows[0] == {"id": "Person-0", "name": rows[0]["name"], "age": rows[0]["age"]}
    assert all(r["id"].startswith("Person-") for r in rows)
    assert all(isinstance(r["age"], int) for r in rows)

def test_node_rows_unique_ids():
    rows = generate_node_rows(NodeSpec("Person", 500, []), seed=1)
    assert len({r["id"] for r in rows}) == 500

def test_seed_determinism():
    a = generate_node_rows(NodeSpec("Person", 50, [Property("v", "float")]), seed=7)
    b = generate_node_rows(NodeSpec("Person", 50, [Property("v", "float")]), seed=7)
    assert a == b
    c = generate_node_rows(NodeSpec("Person", 50, [Property("v", "float")]), seed=8)
    assert a != c

def test_rel_rows_reference_existing_ids():
    counts = {"Person": 100, "Company": 10}
    spec = RelSpec("WORKS_FOR", 90, "Person", "Company", [Property("since", "int")])
    rows = generate_rel_rows(spec, counts, seed=42)
    assert len(rows) == 90
    person_ids = {f"Person-{i}" for i in range(100)}
    company_ids = {f"Company-{i}" for i in range(10)}
    assert all(r["start"] in person_ids and r["end"] in company_ids for r in rows)
    assert all("since" in r for r in rows)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest generator/tests/test_generate.py -v` (or `.venv/bin/pytest ...` if uv not used)
Expected: FAIL — `ModuleNotFoundError: No module named 'generator'`

- [ ] **Step 4: Implement `generator/generate.py`**

```python
"""Synthetic data generation for neo4j-admin database import."""

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

PROPERTY_TYPES = ("string", "int", "float", "bool", "timestamp")


@dataclass(frozen=True)
class Property:
    name: str
    type: str  # one of PROPERTY_TYPES


@dataclass
class NodeSpec:
    label: str
    count: int
    properties: list[Property]


@dataclass
class RelSpec:
    type: str
    count: int
    start: str   # node label
    end: str     # node label
    properties: list[Property]


def load_config(path):
    """Load a JSON config file, returning (node_specs, rel_specs)."""
    with open(path) as f:
        raw = json.load(f)
    nodes = []
    for n in raw.get("nodes", []):
        props = [_prop(n["label"], p) for p in n.get("properties", [])]
        nodes.append(NodeSpec(n["label"], n["count"], props))
    rels = []
    for r in raw.get("relationships", []):
        props = [_prop(f'{r["type"]}', p) for p in r.get("properties", [])]
        rels.append(RelSpec(r["type"], r["count"], r["start"], r["end"], props))
    return nodes, rels


def _prop(owner, raw):
    p = Property(raw["name"], raw["type"])
    if p.type not in PROPERTY_TYPES:
        raise ValueError(
            f"unknown property type '{p.type}' for {owner}.{p.name}; "
            f"expected one of {PROPERTY_TYPES}"
        )
    return p


def _value(rng, kind):
    if kind == "string":
        return f"value-{rng.randrange(10**9)}"
    if kind == "int":
        return rng.randrange(10**6)
    if kind == "float":
        return round(rng.uniform(0, 1000), 3)
    if kind == "bool":
        return rng.choice((True, False))
    if kind == "timestamp":
        return datetime(2020, 1, 1) + timedelta(seconds=rng.randrange(10**8))
    raise ValueError(f"unknown property type: {kind}")


def generate_node_rows(spec, seed):
    """Rows are dicts keyed 'id' plus property names."""
    rng = random.Random(f"{seed}:{spec.label}")
    rows = []
    for i in range(spec.count):
        row = {"id": f"{spec.label}-{i}"}
        for prop in spec.properties:
            row[prop.name] = _value(rng, prop.type)
        rows.append(row)
    return rows


def generate_rel_rows(spec, node_counts, seed):
    """node_counts: {label: count} from the config's node specs."""
    rng = random.Random(f"{seed}:{spec.type}")
    start_count = node_counts[spec.start]
    end_count = node_counts[spec.end]
    rows = []
    for _ in range(spec.count):
        row = {
            "start": f'{spec.start}-{rng.randrange(start_count)}',
            "end": f'{spec.end}-{rng.randrange(end_count)}',
        }
        for prop in spec.properties:
            row[prop.name] = _value(rng, prop.type)
        rows.append(row)
    return rows
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest generator/tests/test_generate.py -v`
Expected: 6 PASS

- [ ] **Step 6: Commit**

```bash
git add generator/ pyproject.toml uv.lock 2>/dev/null || git add generator/ pyproject.toml
git commit -m "feat: generator config loading and synthetic row generation"
```

---

### Task 2: Writers — CSV/Parquet files with admin-import headers, chunking, staging

**Files:**
- Create: `generator/writers.py`
- Create: `generator/tests/test_writers.py`

**Interfaces:**
- Consumes: `Property`, `NodeSpec`, `RelSpec` from `generator/generate.py` (Task 1).
- Produces (imported by Task 3):
  - `ADMIN_TYPES: dict[str, str]` — maps generator type to neo4j-admin header type
  - `node_columns(spec: NodeSpec) -> tuple[list[str], list[str]]` — returns `(header_names, gen_types)`; header positionally aligned with row dict values
  - `rel_columns(spec: RelSpec) -> tuple[list[str], list[str]]`
  - `write_rows(rows: list[dict], header: list[str], gen_types: list[str], out_dir: Path, fmt: str, rows_per_file: int, base_name: str) -> list[Path]` — `fmt` is `"csv"` or `"parquet"`; `rows_per_file=0` means one file; returns written file paths; files written as `<base_name>.<ext>.tmp` then renamed to `<base_name>.<ext>` (single) or `<base_name>-part-<NNNNN>.<ext>` (chunked, NNNNN from 1)

- [ ] **Step 1: Write failing tests**

`generator/tests/test_writers.py`:

```python
import csv
from pathlib import Path

import pyarrow.parquet as pq

from generator.generate import Property, NodeSpec, RelSpec
from generator.writers import node_columns, rel_columns, write_rows, ADMIN_TYPES
from generator.generate import generate_node_rows, generate_rel_rows

NODE = NodeSpec("Person", 250,
                [Property("name", "string"), Property("age", "int"),
                 Property("active", "bool"), Property("ts", "timestamp")])

def test_node_columns_header_syntax():
    header, types = node_columns(NODE)
    assert header == ["id:ID", "name:STRING", "age:INT",
                      "active:BOOLEAN", "ts:DATETIME"]
    assert types == ["id", "string", "int", "bool", "timestamp"]

def test_rel_columns_header_syntax():
    rel = RelSpec("WORKS_FOR", 10, "Person", "Company", [Property("since", "int")])
    header, types = rel_columns(rel)
    assert header == ["start:START_ID", "end:END_ID", "since:INT"]
    assert types == ["start", "end", "int"]

def test_single_file_mode_writes_all_rows(tmp_path):
    rows = generate_node_rows(NODE, seed=1)
    header, types = node_columns(NODE)
    paths = write_rows(rows, header, types, tmp_path, "csv", 0, "person")
    assert len(paths) == 1
    assert paths[0] == tmp_path / "person.csv"
    with open(paths[0], newline="") as f:
        assert sum(1 for _ in csv.reader(f)) == 251  # header + 250 rows

def test_chunked_mode_exact_boundary(tmp_path):
    rows = generate_node_rows(NODE, seed=1)
    header, types = node_columns(NODE)
    # 250 rows / 100 per file -> 3 files (100, 100, 50), no dropped rows
    paths = write_rows(rows, header, types, tmp_path, "csv", 100, "person")
    assert [p.name for p in paths] == [
        "person-part-00001.csv", "person-part-00002.csv", "person-part-00003.csv"]
    total = 0
    for p in paths:
        with open(p, newline="") as f:
            total += sum(1 for _ in csv.reader(f)) - 1
    assert total == 250

def test_chunk_size_larger_than_rows_writes_one_file(tmp_path):
    rows = generate_node_rows(NODE, seed=1)
    header, types = node_columns(NODE)
    paths = write_rows(rows, header, types, tmp_path, "csv", 10_000, "person")
    assert len(paths) == 1 and paths[0].name == "person.csv"

def test_no_tmp_files_left_behind(tmp_path):
    rows = generate_node_rows(NODE, seed=1)
    header, types = node_columns(NODE)
    write_rows(rows, header, types, tmp_path, "csv", 100, "person")
    assert not list(tmp_path.glob("*.tmp"))

def test_parquet_schema_matches_header(tmp_path):
    rows = generate_node_rows(NODE, seed=1)
    header, types = node_columns(NODE)
    paths = write_rows(rows, header, types, tmp_path, "parquet", 0, "person")
    table = pq.read_table(paths[0])
    assert table.column_names == header
    assert table.num_rows == 250

def test_parquet_chunked_same_rows_as_single(tmp_path):
    rows = generate_node_rows(NODE, seed=1)
    header, types = node_columns(NODE)
    single = write_rows(rows, header, types, tmp_path / "a", "parquet", 0, "p")
    chunked = write_rows(rows, header, types, tmp_path / "b", "parquet", 90, "p")
    total = sum(pq.read_table(p).num_rows for p in chunked)
    assert total == pq.read_table(single[0]).num_rows == 250

def test_unknown_format_raises(tmp_path):
    rows = generate_node_rows(NODE, seed=1)
    header, types = node_columns(NODE)
    import pytest
    with pytest.raises(ValueError, match="unknown format"):
        write_rows(rows, header, types, tmp_path, "orc", 0, "person")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest generator/tests/test_writers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'generator.writers'`

- [ ] **Step 3: Implement `generator/writers.py`**

```python
"""File writers producing neo4j-admin import compatible CSV and Parquet."""

import csv
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from generator.generate import NodeSpec, RelSpec

ADMIN_TYPES = {
    "string": "STRING",
    "int": "INT",
    "float": "FLOAT",
    "bool": "BOOLEAN",
    "timestamp": "DATETIME",
}

ARROW_TYPES = {
    "id": pa.string(),
    "start": pa.string(),
    "end": pa.string(),
    "string": pa.string(),
    "int": pa.int64(),
    "float": pa.float64(),
    "bool": pa.bool_(),
    "timestamp": pa.timestamp("ms"),
}


def node_columns(spec: NodeSpec):
    header = ["id:ID"]
    types = ["id"]
    for prop in spec.properties:
        header.append(f"{prop.name}:{ADMIN_TYPES[prop.type]}")
        types.append(prop.type)
    return header, types


def rel_columns(spec: RelSpec):
    header = ["start:START_ID", "end:END_ID"]
    types = ["start", "end"]
    for prop in spec.properties:
        header.append(f"{prop.name}:{ADMIN_TYPES[prop.type]}")
        types.append(prop.type)
    return header, types


def _keys(header):
    """Row-dict keys positionally aligned with header names."""
    keys = []
    for h in header:
        name = h.split(":", 1)[0]
        keys.append({"id": "id", "start": "start", "end": "end"}.get(name, name))
    return keys


def _chunks(rows, rows_per_file):
    if rows_per_file <= 0 or rows_per_file >= len(rows):
        yield rows
        return
    for i in range(0, len(rows), rows_per_file):
        yield rows[i:i + rows_per_file]


def _csv_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _write_csv(chunk, header, keys, path: Path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in chunk:
            writer.writerow([_csv_value(row[k]) for k in keys])
    tmp.rename(path)


def _write_parquet(chunk, header, keys, gen_types, path: Path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = {h: pa.array([row[k] for row in chunk], type=ARROW_TYPES[t])
            for h, k, t in zip(header, keys, gen_types)}
    pq.write_table(pa.table(data), tmp)
    tmp.rename(path)


def write_rows(rows, header, gen_types, out_dir, fmt, rows_per_file, base_name):
    """Write rows to one or more files. Returns list of final file paths.

    Files are written with a .tmp suffix and renamed only when complete,
    so consumers never observe half-written files.
    """
    if fmt not in ("csv", "parquet"):
        raise ValueError(f"unknown format: {fmt}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = _keys(header)
    ext = fmt
    paths = []
    for i, chunk in enumerate(_chunks(rows, rows_per_file), start=1):
        if rows_per_file <= 0 or rows_per_file >= len(rows):
            name = f"{base_name}.{ext}"
        else:
            name = f"{base_name}-part-{i:05d}.{ext}"
        path = out_dir / name
        if fmt == "csv":
            _write_csv(chunk, header, keys, path)
        else:
            _write_parquet(chunk, header, keys, gen_types, path)
        paths.append(path)
    return paths
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest generator/tests/test_writers.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add generator/writers.py generator/tests/test_writers.py
git commit -m "feat: CSV/Parquet writers with admin-import headers and chunking"
```

---

### Task 3: Destination publishing and CLI entry point

**Files:**
- Create: `generator/publish.py`
- Create: `generator/cli.py`
- Create: `generator/__main__.py`
- Create: `generator/tests/test_publish.py`
- Create: `generator/tests/test_cli.py`
- Modify: `pyproject.toml` (console script)
- Create: `configs/example.json` (example config, doubles as smoke-test input)

**Interfaces:**
- Consumes: `load_config`, `generate_node_rows`, `generate_rel_rows`, specs from Task 1; `node_columns`, `rel_columns`, `write_rows` from Task 2.
- Produces:
  - `generator/publish.py`:
    - `is_s3_dest(dest: str) -> bool`
    - `parse_s3(dest: str) -> tuple[str, str]` — `(bucket, key_prefix)`; raises `ValueError` on malformed
    - `publish(paths: list[Path], dest: str) -> list[str]` — copy files into a local directory tree, or upload to S3; returns final destinations (local paths as `str`, S3 as `s3://bucket/key`)
  - `generator/cli.py`:
    - `main(argv: list[str] | None = None) -> int` — returns exit code
    - CLI flags: `--config PATH` (required), `--format csv|parquet` (default `csv`), `--rows-per-file N` (default 0 = one file per label), `--seed N` (default 42), `--dest DEST` (required; local dir path or `s3://bucket/prefix/`)
    - Prints a JSON manifest to stdout: `{"nodes": {label: [dests]}, "relationships": {type: [dests]}, "rows": {"nodes": N, "relationships": M}}`
    - Layout under dest: `nodes/<Label>/...`, `relationships/<TYPE>/...`
  - `python -m generator` and console script `generate` both invoke `main()`

- [ ] **Step 1: Write failing tests for publish**

`generator/tests/test_publish.py`:

```python
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from generator.publish import is_s3_dest, parse_s3, publish


def test_is_s3_dest():
    assert is_s3_dest("s3://bucket/prefix/")
    assert not is_s3_dest("/local/dir")
    assert not is_s3_dest("bucket/prefix")

def test_parse_s3():
    assert parse_s3("s3://my-bucket/src/nodes/") == ("my-bucket", "src/nodes")
    with pytest.raises(ValueError, match="s3://bucket/prefix"):
        parse_s3("s3://")

def test_publish_local_moves_files(tmp_path):
    src = tmp_path / "stage"
    src.mkdir()
    f = src / "person.csv"
    f.write_text("id:ID\nPerson-0\n")
    dests = publish([f], str(tmp_path / "out"))
    assert dests == [str(tmp_path / "out" / "person.csv")]
    assert (tmp_path / "out" / "person.csv").read_text() == "id:ID\nPerson-0\n"
    assert not f.exists()  # moved, not copied — stage dir is disposable

@mock_aws
def test_publish_s3_uploads(tmp_path):
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.create_bucket(
        Bucket="test-bucket",
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    f = tmp_path / "person-part-00001.csv"
    f.write_text("id:ID\nPerson-0\n")
    dests = publish([f], "s3://test-bucket/source/nodes/Person/")
    assert dests == ["s3://test-bucket/source/nodes/Person/person-part-00001.csv"]
    obj = s3.get_object(Bucket="test-bucket",
                        Key="source/nodes/Person/person-part-00001.csv")
    assert obj["Body"].read().decode() == "id:ID\nPerson-0\n"

@mock_aws
def test_publish_s3_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        publish([tmp_path / "nope.csv"], "s3://any-bucket/x/")
```

- [ ] **Step 2: Write failing tests for CLI**

`generator/tests/test_cli.py`:

```python
import json

import boto3
import pytest
from moto import mock_aws

from generator.cli import main

CONFIG = {
    "nodes": [
        {"label": "Person", "count": 50,
         "properties": [{"name": "name", "type": "string"}]},
        {"label": "Company", "count": 5, "properties": []},
    ],
    "relationships": [
        {"type": "WORKS_FOR", "count": 45, "start": "Person",
         "end": "Company", "properties": []},
    ],
}

@pytest.fixture
def config_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(CONFIG))
    return str(p)


def test_cli_local_dest_writes_layout_and_manifest(tmp_path, config_file, capsys):
    dest = tmp_path / "source"
    rc = main(["--config", config_file, "--dest", str(dest),
               "--format", "csv", "--rows-per-file", "20", "--seed", "9"])
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["rows"] == {"nodes": 55, "relationships": 45}
    assert len(manifest["nodes"]["Person"]) == 3   # 50 rows / 20 per file
    assert len(manifest["nodes"]["Company"]) == 1
    assert (dest / "nodes" / "Person" / "person-part-00001.csv").exists()
    assert (dest / "relationships" / "WORKS_FOR" / "works_for.csv").exists()


def test_cli_parquet_single_file(tmp_path, config_file, capsys):
    dest = tmp_path / "source"
    rc = main(["--config", config_file, "--dest", str(dest),
               "--format", "parquet"])
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)
    assert len(manifest["nodes"]["Person"]) == 1
    assert (dest / "nodes" / "Person" / "person.parquet").exists()


@mock_aws
def test_cli_s3_dest_uploads(tmp_path, config_file, capsys):
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.create_bucket(
        Bucket="test-bucket",
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    rc = main(["--config", config_file, "--dest", "s3://test-bucket/source/",
               "--format", "csv"])
    assert rc == 0
    keys = {o["Key"] for o in
            s3.list_objects_v2(Bucket="test-bucket", Prefix="source/")["Contents"]}
    assert "source/nodes/Person/person.csv" in keys
    assert "source/relationships/WORKS_FOR/works_for.csv" in keys


def test_cli_seed_determinism_same_manifest(tmp_path, config_file):
    outs = []
    for _ in range(2):
        rc = main(["--config", config_file, "--dest", str(tmp_path / "s"),
                   "--seed", "5"])
        assert rc == 0
    # same config + seed: second run overwrites same file set, no error
    assert (tmp_path / "s" / "nodes" / "Person" / "person.csv").exists()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest generator/tests/test_publish.py generator/tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'generator.publish'`

- [ ] **Step 4: Implement `generator/publish.py`**

```python
"""Publish staged output files to a local directory or S3."""

import shutil
from pathlib import Path

import boto3


def is_s3_dest(dest: str) -> bool:
    return dest.startswith("s3://")


def parse_s3(dest: str):
    rest = dest[len("s3://"):]
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError(f"malformed S3 destination: {dest!r}; expected s3://bucket/prefix")
    return bucket, prefix.rstrip("/")


def _s3_client():
    return boto3.client("s3")


def publish(paths, dest: str):
    """Move staged files into dest (local dir or s3:// prefix).

    Uploads happen only from fully written local files, so S3 consumers
    never observe partial objects.
    """
    if is_s3_dest(dest):
        bucket, prefix = parse_s3(dest)
        client = _s3_client()
        out = []
        for path in paths:
            path = Path(path)
            if not path.is_file():
                raise FileNotFoundError(f"staged file missing: {path}")
            key = f"{prefix}/{path.name}" if prefix else path.name
            client.upload_file(str(path), bucket, key)
            out.append(f"s3://{bucket}/{key}")
        return out
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"staged file missing: {path}")
        target = dest_dir / path.name
        shutil.move(str(path), target)
        out.append(str(target))
    return out
```

- [ ] **Step 5: Implement `generator/cli.py` and `generator/__main__.py`**

`generator/cli.py`:

```python
"""Command line entry point for the synthetic data generator."""

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from generator.generate import (
    generate_node_rows, generate_rel_rows, load_config,
)
from generator.publish import publish
from generator.writers import node_columns, rel_columns, write_rows


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name).lower()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate synthetic neo4j-admin import source data")
    parser.add_argument("--config", required=True, help="JSON config path")
    parser.add_argument("--format", choices=("csv", "parquet"), default="csv")
    parser.add_argument("--rows-per-file", type=int, default=0,
                        help="rows per file; 0 = one file per label/type")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dest", required=True,
                        help="local directory or s3://bucket/prefix/")
    args = parser.parse_args(argv)

    nodes, rels = load_config(args.config)
    node_counts = {n.label: n.count for n in nodes}
    base_dest = args.dest.rstrip("/")
    stage = Path(tempfile.mkdtemp(prefix="neo4j-gen-"))
    manifest = {"nodes": {}, "relationships": {}, "rows": {
        "nodes": 0, "relationships": 0}}
    try:
        for spec in nodes:
            rows = generate_node_rows(spec, args.seed)
            header, types = node_columns(spec)
            paths = write_rows(rows, header, types, stage / "nodes" / spec.label,
                               args.format, args.rows_per_file,
                               _slug(spec.label))
            manifest["nodes"][spec.label] = publish(
                paths, f"{base_dest}/nodes/{spec.label}")
            manifest["rows"]["nodes"] += spec.count
        for spec in rels:
            rows = generate_rel_rows(spec, node_counts, args.seed)
            header, types = rel_columns(spec)
            paths = write_rows(rows, header, types,
                               stage / "relationships" / spec.type,
                               args.format, args.rows_per_file, _slug(spec.type))
            manifest["relationships"][spec.type] = publish(
                paths, f"{base_dest}/relationships/{spec.type}")
            manifest["rows"]["relationships"] += spec.count
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`generator/__main__.py`:

```python
from generator.cli import main
import sys

sys.exit(main())
```

Add to `pyproject.toml`:

```toml
[project.scripts]
generate = "generator.cli:main"
```

- [ ] **Step 6: Create example config `configs/example.json`**

```json
{
  "nodes": [
    {"label": "Person", "count": 100000,
     "properties": [
       {"name": "name", "type": "string"},
       {"name": "age", "type": "int"},
       {"name": "active", "type": "bool"},
       {"name": "created", "type": "timestamp"}
     ]},
    {"label": "Company", "count": 10000,
     "properties": [
       {"name": "name", "type": "string"},
       {"name": "revenue", "type": "float"}
     ]}
  ],
  "relationships": [
    {"type": "WORKS_FOR", "count": 90000, "start": "Person", "end": "Company",
     "properties": [{"name": "since", "type": "int"}]},
    {"type": "KNOWS", "count": 200000, "start": "Person", "end": "Person",
     "properties": [{"name": "strength", "type": "float"}]}
  ]
}
```

- [ ] **Step 7: Run all generator tests**

Run: `uv run pytest -v`
Expected: all PASS (Tasks 1-3)

- [ ] **Step 8: Manual smoke check (local, no AWS)**

Run: `uv run generate --config configs/example.json --dest /tmp/gen-smoke --format parquet --rows-per-file 25000`
Expected: JSON manifest on stdout; `/tmp/gen-smoke/nodes/Person/` contains 4 parquet parts; then delete `/tmp/gen-smoke`.

- [ ] **Step 9: Commit**

```bash
git add generator/ configs/ pyproject.toml uv.lock 2>/dev/null || git add generator/ configs/ pyproject.toml
git commit -m "feat: generator CLI with local and S3 destinations"
```

---

### Task 4: Import runner script

**Files:**
- Create: `scripts/run_import.sh`
- Create: `scripts/tests/test_run_import.py`
- Create: `scripts/tests/stubs/` (test fixtures, created by the test itself)

**Interfaces:**
- Consumes: data layout produced by Task 3 CLI: `nodes/<Label>/*.{csv,parquet}` and `relationships/<TYPE>/*.{csv,parquet}` under the source prefix.
- Produces:
  - `scripts/run_import.sh` with flags: `--bucket BUCKET` (required), `--mode s3|local` (required), `--db NAME` (default `neo4j`), `--dry-run`, `--keep-scratch` (default: local mode deletes `/scratch/source` after import)
  - Environment overrides (used by tests and perf tuning): `JAVA_HEAP` (default `8g`), `NEO4J_HOME` (default `/opt/neo4j`), `SCRATCH` (default `/scratch`), `ZSTD_LEVEL` (default `3`)
  - Behavior: discovers node labels and rel types by listing `nodes/` and `relationships/` prefixes; builds `neo4j-admin database import full <db> --nodes=<Label>=<files> --relationships=<TYPE>=<files> --verbose` with comma-joined file lists; local mode syncs source to `$SCRATCH/source` first; after import, tars `$NEO4J_HOME/data/databases/<db>` to `$SCRATCH/databases/<db>-<timestamp>.tar.zst`, uploads to `s3://<bucket>/databases/`, verifies remote size == local size, writes timings JSON to `s3://<bucket>/timings/<db>-<timestamp>.json`; any failure exits non-zero with a message; `--dry-run` prints the resolved command and exits 0 without running anything.

- [ ] **Step 1: Write failing tests**

`scripts/tests/test_run_import.py`:

```python
import json
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
    (scratch / "source" / "nodes" / "Person").mkdir(parents=True)
    (scratch / "source" / "relationships" / "WORKS_FOR").mkdir(parents=True)
    (scratch / "source" / "nodes" / "Person" / "person.csv").write_text(
        "id:ID\nPerson-0\n")
    (scratch / "source" / "relationships" / "WORKS_FOR" /
     "works_for.csv").write_text("start:START_ID,end:END_ID\n")

    # fake neo4j-admin: record args, create a fake db dir, exit 0
    admin = stub_dir / "neo4j-admin"
    admin.write_text(f"""#!/bin/bash
echo "$@" > "{tmp_path / 'admin-args.txt'}"
mkdir -p "{home}/data/databases/testdb"
echo blob > "{home}/data/databases/testdb/file.db"
exit 0
""")
    # fake aws: emulate ls/cp; size check reads from fake_s3
    aws = stub_dir / "aws"
    aws.write_text(f"""#!/bin/bash
cmd="$1"; shift
case "$cmd" in
  ls)
    # aws s3 ls --recursive s3://b/source/   -> file listing
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
        s3://b/databases/*|s3://b/timings/*)
          exit 0
          ;;
      esac
    done
    ;;
  s3)
    sub="$1"; shift
    if [ "$sub" = "sync" ]; then exit 0; fi
    if [ "$sub" = "cp" ]; then
      src="$1"; dest="$2"
      if [[ "$dest" == s3://* ]]; then
        cp "$src" "{fake_s3}/$(basename "$dest")"
        size=$(stat -c%s "{fake_s3}/$(basename "$dest")" 2>/dev/null || echo 0)
        echo "$size $dest"
        exit 0
      else
        cp "$src" "$dest"; exit 0
      fi
    fi
    ;;
esac
exit 1
""")
    for stub in (admin, aws):
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    e = dict(os.environ)
    e.update(PATH=str(stub_dir), NEO4J_HOME=str(home), SCRATCH=str(scratch),
             BASH_ENV="", LC_ALL="C")
    return type("Env", (), {
        "env": e, "tmp": tmp_path, "fake_s3": fake_s3, "home": home,
        "scratch": scratch})()


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
    aws_stub.write_text(original.replace(
        'size=$(stat -c%s "{fake_s3}/$(basename "$dest")" 2>/dev/null || echo 0)',
        'size=999999'))
    aws_stub.chmod(aws_stub.stat().st_mode | stat.S_IEXEC)
    r = run_script(env, "--bucket", "b", "--mode", "local", "--db", "testdb")
    assert r.returncode != 0
    assert "size mismatch" in (r.stdout + r.stderr).lower()
```

Note for the implementer: in the fake `aws` script, `{fake_s3}` and `{home}` are substituted by the f-string at fixture creation; keep them as literal interpolated values.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/tests/test_run_import.py -v`
Expected: FAIL — `No such file or directory: .../scripts/run_import.sh` or returncode != 0

- [ ] **Step 3: Implement `scripts/run_import.sh`**

```bash
#!/usr/bin/env bash
# Run neo4j-admin database import from S3 or local scratch, then upload the
# resulting database artifact back to S3 (mandatory, size-verified).
set -euo pipefail

BUCKET="" MODE="" DB="neo4j" DRY_RUN=0 KEEP_SCRATCH=0
JAVA_HEAP="${JAVA_HEAP:-8g}"
NEO4J_HOME="${NEO4J_HOME:-/opt/neo4j}"
SCRATCH="${SCRATCH:-/scratch}"
ZSTD_LEVEL="${ZSTD_LEVEL:-3}"
SOURCE_NODES="nodes" SOURCE_RELS="relationships"

usage() {
  cat <<EOF
Usage: $(basename "$0") --bucket BUCKET --mode s3|local [--db NAME]
                      [--dry-run] [--keep-scratch]

Environment: JAVA_HEAP (default 8g), NEO4J_HOME (default /opt/neo4j),
             SCRATCH (default /scratch), ZSTD_LEVEL (default 3)
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket) BUCKET="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --keep-scratch) KEEP_SCRATCH=1; shift ;;
    *) usage ;;
  esac
done
[[ -n "$BUCKET" && ( "$MODE" == "s3" || "$MODE" == "local" ) ]] || usage

S3_SOURCE="s3://$BUCKET/source"
DB_DIR="$NEO4J_HOME/data/databases/$DB"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
ARTIFACT_NAME="${DB}-${TIMESTAMP}.tar.zst"
ARTIFACT="$SCRATCH/databases/$ARTIFACT_NAME"
S3_DB_DEST="s3://$BUCKET/databases/$ARTIFACT_NAME"
S3_TIMINGS_DEST="s3://$BUCKET/timings/${DB}-${TIMESTAMP}.json"
fail() { echo "ERROR: $*" >&2; exit 1; }

# --- source file listing ----------------------------------------------------
s3_file_list() { # $1 = s3 dir; prints full s3:// URLs of contained files
  aws s3 ls --recursive "$1" | awk -v prefix="s3://$BUCKET/" '{print prefix $4}'
}
local_file_list() { # $1 = local dir
  find "$1" -type f | sort
}

if [[ "$MODE" == "s3" ]]; then
  NODE_LABELS=$(aws s3 ls "$S3_SOURCE/$SOURCE_NODES/" | awk '/PRE /{print $2}' | tr -d '/' | tr '\n' ' ')
  REL_TYPES=$(aws s3 ls "$S3_SOURCE/$SOURCE_RELS/" | awk '/PRE /{print $2}' | tr -d '/' | tr '\n' ' ')
else
  mkdir -p "$SCRATCH/source"
  T_SYNC0=$(date +%s)
  aws s3 sync "$S3_SOURCE/" "$SCRATCH/source/"
  T_SYNC1=$(date +%s)
  NODE_LABELS=$(cd "$SCRATCH/source/$SOURCE_NODES" 2>/dev/null && ls -1 || true)
  REL_TYPES=$(cd "$SCRATCH/source/$SOURCE_RELS" 2>/dev/null && ls -1 || true)
fi

[[ -n "$NODE_LABELS" ]] || fail "no source node files found under $S3_SOURCE/$SOURCE_NODES/ — run the generator first"

IMPORT_ARGS=(--verbose)
for label in $NODE_LABELS; do
  if [[ "$MODE" == "s3" ]]; then
    files=$(s3_file_list "$S3_SOURCE/$SOURCE_NODES/$label/")
  else
    files=$(local_file_list "$SCRATCH/source/$SOURCE_NODES/$label")
  fi
  [[ -n "$files" ]] || fail "no files for node label $label"
  IMPORT_ARGS+=(--nodes="$label=$(echo "$files" | paste -sd, -)")
done
for rtype in $REL_TYPES; do
  if [[ "$MODE" == "s3" ]]; then
    files=$(s3_file_list "$S3_SOURCE/$SOURCE_RELS/$rtype/")
  else
    files=$(local_file_list "$SCRATCH/source/$SOURCE_RELS/$rtype")
  fi
  [[ -n "$files" ]] || fail "no files for relationship type $rtype"
  IMPORT_ARGS+=(--relationships="$rtype=$(echo "$files" | paste -sd, -)")
done

CMD=(neo4j-admin database import full "$DB" "${IMPORT_ARGS[@]}")

if [[ "$DRY_RUN" == 1 ]]; then
  echo "DRY RUN resolved command:"
  printf '  %q ' "${CMD[@]}"; echo
  echo "DRY RUN artifact destination: $S3_DB_DEST"
  exit 0
fi

# --- import -------------------------------------------------------------------
T_IMPORT0=$(date +%s)
JAVA_HEAP="$JAVA_HEAP" "${CMD[@]}" || fail "neo4j-admin import failed (exit $?); source kept at $SCRATCH/source for post-mortem"
T_IMPORT1=$(date +%s)
[[ -d "$DB_DIR" ]] || fail "import succeeded but database dir not found: $DB_DIR"

# --- artifact upload (mandatory) ----------------------------------------------
mkdir -p "$SCRATCH/databases"
T_PACK0=$(date +%s)
tar -C "$DB_DIR" -cf - . | zstd -"$ZSTD_LEVEL" -o "$ARTIFACT" \
  || fail "artifact packing failed"
T_PACK1=$(date +%s)
aws s3 cp "$ARTIFACT" "$S3_DB_DEST" || fail "artifact upload failed"
LOCAL_SIZE=$(stat -c%s "$ARTIFACT")
REMOTE_SIZE=$(aws s3 ls "$S3_DB_DEST" | awk '{print $3}')
[[ -n "$REMOTE_SIZE" && "$REMOTE_SIZE" == "$LOCAL_SIZE" ]] \
  || fail "artifact upload size mismatch: local=$LOCAL_SIZE remote=${REMOTE_SIZE:-none}"

# --- timings ------------------------------------------------------------------
T_TOTAL=$(date +%s)
TIMINGS_FILE="$SCRATCH/databases/${DB}-${TIMESTAMP}.json"
cat > "$TIMINGS_FILE" <<EOF
{"db": "$DB", "mode": "$MODE", "bucket": "$BUCKET",
 "java_heap": "$JAVA_HEAP", "timestamp": "$TIMESTAMP",
 "sync_seconds": ${T_SYNC1:-0}-${T_SYNC0:-0},
 "import_seconds": $((T_IMPORT1 - T_IMPORT0)),
 "pack_seconds": $((T_PACK1 - T_PACK0)),
 "total_seconds": $((T_TOTAL - ${T_SYNC0:-T_IMPORT0})),
 "artifact_bytes": $LOCAL_SIZE,
 "artifact": "$S3_DB_DEST"}
EOF
aws s3 cp "$TIMINGS_FILE" "$S3_TIMINGS_DEST" || fail "timings upload failed"

if [[ "$MODE" == "local" && "$KEEP_SCRATCH" != 1 ]]; then
  rm -rf "$SCRATCH/source"
fi

echo "OK: import complete, artifact at $S3_DB_DEST, timings at $S3_TIMINGS_DEST"
```

Note: `"sync_seconds": ${T_SYNC1:-0}-${T_SYNC0:-0}` is arithmetic inside JSON — implementer should compute the value first (`SYNC_SECONDS=$(( ${T_SYNC1:-${T_SYNC0:-0}} - ${T_SYNC0:-0} ))`) and emit `"$SYNC_SECONDS"`; verify the JSON parses in the test by loading `testdb-*.json` from fake S3 with `json.loads`.

Make it executable: `chmod +x scripts/run_import.sh`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/tests/test_run_import.py -v`
Expected: 5 PASS

- [ ] **Step 5: Run full suite**

Run: `uv run pytest -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/run_import.sh scripts/tests/
git commit -m "feat: import runner with s3/local modes, artifact upload, timings"
```

---

### Task 5: Terraform root module

**Files:**
- Create: `terraform/main.tf`
- Create: `terraform/variables.tf`
- Create: `terraform/outputs.tf`
- Create: `terraform/user_data.sh.tpl`
- Create: `terraform/versions.tf`

**Interfaces:**
- Consumes: nothing from other tasks (infra only).
- Produces: AWS resources — S3 bucket (name in output `bucket_name`), EC2 instance (`instance_public_ip`, `ssh_command` outputs), IAM instance profile with S3-only access, security group allowing SSH from `allowed_ssh_cidr`. `user_data.sh.tpl` renders `neo4j_version` variable; installs Java 21, Neo4j Enterprise tarball, RAID0-mounts NVMe at `/scratch`, accepts Neo4j Enterprise license, writes marker `/opt/neo4j/.setup-complete`.

- [ ] **Step 1: Write `terraform/versions.tf`**

```hcl
terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}
```

- [ ] **Step 2: Write `terraform/variables.tf`**

```hcl
variable "region" {
  description = "AWS region"
  type        = string
  default     = "eu-west-1"
}

variable "instance_type" {
  description = "EC2 instance type for the import runner"
  type        = string
  default     = "m7gd.4xlarge"
}

variable "neo4j_version" {
  description = "Neo4j Enterprise version (tarball from dist.neo4j.org)"
  type        = string
  default     = "2026.10.1"
}

variable "bucket_name" {
  description = "S3 bucket name prefix; a random hex suffix is appended"
  type        = string
  default     = "neo4j-import-tests"
}

variable "root_volume_size" {
  description = "EBS root volume size in GB (Neo4j install + tooling)"
  type        = number
  default     = 100
}

variable "allowed_ssh_cidr" {
  description = "CIDR allowed to SSH to the instance (e.g. your_ip/32)"
  type        = string
}
```

- [ ] **Step 3: Write `terraform/main.tf`**

```hcl
provider "aws" {
  region = var.region
}

# --- S3 bucket ----------------------------------------------------------------

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "data" {
  bucket        = "${var.bucket_name}-${random_id.bucket_suffix.hex}"
  force_destroy = true # bucket holds only generated test data
}

# --- IAM: instance profile with S3-only access ----------------------------------

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "import_runner" {
  name               = "neo4j-import-runner"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

data "aws_iam_policy_document" "s3_access" {
  statement {
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.data.arn,
      "${aws_s3_bucket.data.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "s3_access" {
  name   = "s3-access"
  role   = aws_iam_role.import_runner.id
  policy = data.aws_iam_policy_document.s3_access.json
}

resource "aws_iam_instance_profile" "import_runner" {
  name = "neo4j-import-runner"
  role = aws_iam_role.import_runner.name
}

# --- Networking: SSH-only security group ----------------------------------------

resource "aws_security_group" "ssh" {
  name        = "neo4j-import-ssh"
  description = "SSH only, from operator CIDR"

  ingress {
    description = "SSH from operator"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  egress {
    description = "all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- EC2 instance ---------------------------------------------------------------

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_instance" "import_runner" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  iam_instance_profile   = aws_iam_instance_profile.import_runner.name
  vpc_security_group_ids = [aws_security_group.ssh.id]

  root_block_device {
    volume_size = var.root_volume_size
    volume_type = "gp3"
  }

  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    neo4j_version = var.neo4j_version
  })

  tags = {
    Name    = "neo4j-import-runner"
    Project = "neo4j-admin-s3-import-tests"
  }
}
```

- [ ] **Step 4: Write `terraform/outputs.tf`**

```hcl
output "bucket_name" {
  description = "S3 bucket for source data, artifacts, and timings"
  value       = aws_s3_bucket.data.bucket
}

output "instance_public_ip" {
  description = "EC2 import runner public IP"
  value       = aws_instance.import_runner.public_ip
}

output "ssh_command" {
  description = "SSH into the import runner"
  value       = "ssh ec2-user@${aws_instance.import_runner.public_ip}"
}
```

- [ ] **Step 5: Write `terraform/user_data.sh.tpl`**

```bash
#!/bin/bash
# Installs Java 21 + Neo4j Enterprise, RAID0-mounts NVMe scratch at /scratch.
set -euo pipefail
exec > >(tee /var/log/neo4j-setup.log) 2>&1

NEO4J_VERSION="${neo4j_version}"
NEO4J_HOME=/opt/neo4j

dnf install -y java-21-amazon-corretto-headless tar zstd awscli mdadm xfsprogs

# RAID0 all instance-store NVMe devices into /scratch
DEVICES=$(ls /dev/nvme[0-9]*n1 2>/dev/null || true)
if [[ -n "$DEVICES" ]]; then
  N=$(echo $DEVICES | wc -w)
  mdadm --create /dev/md0 --level=0 --raid-devices="$N" $DEVICES --force
  mkfs.xfs /dev/md0
  mkdir -p /scratch
  echo '/dev/md0 /scratch xfs defaults,nofail 0 2' >> /etc/fstab
  mount /scratch
else
  mkdir -p /scratch # fallback: no NVMe found (wrong instance family?)
fi

mkdir -p /scratch/source /scratch/databases "$NEO4J_HOME"
chown -R ec2-user:ec2-user /scratch "$NEO4J_HOME"

curl -fsSL "https://dist.neo4j.org/neo4j-enterprise-$NEO4J_VERSION-unix.tar.gz" \
  -o /tmp/neo4j.tgz
tar -xzf /tmp/neo4j.tgz -C "$NEO4J_HOME" --strip-components=1
"$NEO4J_HOME/bin/neo4j-admin" server license accept

touch "$NEO4J_HOME/.setup-complete"
echo "setup complete"
```

- [ ] **Step 6: Verify with terraform validate**

Install if missing: `brew install terraform` (or `brew install opentofu` and substitute).
Run: `cd terraform && terraform init && terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 7: Verify with terraform plan (requires AWS session)**

Operator: re-authenticate first (`! aws login` in this session).
Run: `cd terraform && terraform plan -var 'allowed_ssh_cidr=<operator_ip>/32'`
Expected: plan shows 1 bucket, 1 instance, IAM role/profile, SG; no errors, no unexpected resources. Do NOT apply yet — apply is the Task 6 manual gate.

- [ ] **Step 8: Commit**

```bash
git add terraform/
git commit -m "feat: Terraform root module for EC2 import runner and S3 bucket"
```

---

### Task 6: README workflow and manual smoke-test gate

**Files:**
- Create: `README.md`
- Modify: none

**Interfaces:**
- Consumes: all previous tasks.
- Produces: documented operator workflow; executed smoke test proving the system end-to-end (the only real-AWS verification in this plan).

- [ ] **Step 1: Write `README.md`**

```markdown
# neo4j-admin S3 import tests

AWS infrastructure for running `neo4j-admin database import` performance
tests: one EC2 instance, one S3 bucket, synthetic data generation, and a
switchable S3-direct vs local-NVMe import mode.

See `docs/superpowers/specs/2026-11-03-aws-neo4j-import-infra-design.md`
for the design.

## Layout

- `terraform/` — root module: S3 bucket, m7gd.4xlarge EC2, IAM, user_data
- `generator/` — synthetic CSV/Parquet data generator
- `scripts/run_import.sh` — import runner (s3 | local mode), artifact upload
- `configs/example.json` — example generator config

## Workflow

```bash
# 0. Authenticate
aws login

# 1. Provision (~3-4 min)
cd terraform
terraform init
terraform apply -var 'allowed_ssh_cidr=<your_ip>/32'
# note bucket_name and ssh_command from outputs

# 2. SSH in
ssh ec2-user@<ip>
# wait for setup: until [ -f /opt/neo4j/.setup-complete ]; do sleep 5; done

# 3. Generate source data (on the instance, or anywhere with AWS creds)
generate --config configs/example.json --dest s3://<bucket>/source/ \
  --format csv --rows-per-file 100000

# 4. Run import — S3-direct:
scripts/run_import.sh --bucket <bucket> --mode s3
#    or local-NVMe:
scripts/run_import.sh --bucket <bucket> --mode local

# 5. Results: s3://<bucket>/databases/<db>-<ts>.tar.zst and
#    s3://<bucket>/timings/<db>-<ts>.json

# 6. Tear down between iterations
terraform destroy -var 'allowed_ssh_cidr=<your_ip>/32'
```

## Testing

```bash
uv run pytest          # generator + runner unit tests, no AWS needed
cd terraform && terraform validate
```

Note: the bucket has `force_destroy = true` — `terraform destroy` deletes
all objects in it. It holds only generated test data.
```

- [ ] **Step 2: Manual smoke gate — real apply (operator + implementer together)**

Prerequisite: valid AWS session; `scp` or `git clone` of this repo onto the instance after apply.

1. `cd terraform && terraform apply -var 'allowed_ssh_cidr=<operator_ip>/32'`
2. SSH in; wait for `/opt/neo4j/.setup-complete`; check `/var/log/neo4j-setup.log` for errors; verify `df -h /scratch` shows the RAID0 NVme volume.
3. Copy repo to instance: `scp -r . ec2-user@<ip>:~/` (or git clone).
4. Install generator on instance: `uv sync` (or `pip install .`) in the repo dir.
5. Generate small dataset: `generate --config configs/example.json --dest s3://<bucket>/source/ --format csv` (first reduce counts in a copy of the config to ~10k nodes for speed).
6. `scripts/run_import.sh --bucket <bucket> --mode s3 --db smoketest`
   - If neo4j-admin fails on S3 credentials: the admin tool must pick up the instance profile via the default credential chain (IMDS). If it does not, set explicit `AWS_*` env vars or `~/.aws/credentials` from the instance profile via `aws configure list` debug, and record the exact fix in the README (this is the spec's flagged verification point).
   - Verify exact `neo4j-admin database import full` flag support for 2026.10.1 (`--verbose`, `--nodes`, `--relationships` comma lists); adjust if flag syntax differs and note in README.
7. Verify `s3://<bucket>/databases/smoketest-<ts>.tar.zst` exists and `s3://<bucket>/timings/smoketest-<ts>.json` parses.
8. Repeat with `--mode local`; verify `/scratch/source` is removed after success.
9. `terraform destroy -var 'allowed_ssh_cidr=<operator_ip>/32'`; verify bucket and instance gone.

Expected: both modes complete; artifacts and timings in S3; clean teardown.

- [ ] **Step 3: Record any deviations in README, run full test suite**

Run: `uv run pytest && (cd terraform && terraform validate)`
Expected: all PASS, validate Success.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: operator workflow and smoke-test runbook"
```

---

## Self-Review Notes

- Spec coverage: Terraform infra (Task 5), generator CSV/Parquet + chunking + seed (Tasks 1-3), both import modes + mandatory artifact upload + timings (Task 4), error handling (fail-fast everywhere; empty-source, size-mismatch, unknown-type tests), testing strategy (Tasks 1-4 unit tests, Tasks 5-6 validate/plan + manual gate), perf-test hooks (timings prefix). Bucket `force_destroy` flagged in README.
- Known verification points deliberately deferred to the Task 6 smoke gate: (a) neo4j-admin S3 credential pickup via instance profile, (b) exact 2026.10.1 admin import flag syntax, (c) `neo4j-admin server license accept` command name. These are marked in-line.
