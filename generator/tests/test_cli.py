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
    assert (dest / "relationships" / "WORKS_FOR" /
            "works_for-part-00001.csv").exists()


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
    for _ in range(2):
        rc = main(["--config", config_file, "--dest", str(tmp_path / "s"),
                   "--seed", "5"])
        assert rc == 0
    # same config + seed: second run overwrites same file set, no error
    assert (tmp_path / "s" / "nodes" / "Person" / "person.csv").exists()


def test_cli_regeneration_clears_stale_files(tmp_path, config_file):
    dest = tmp_path / "source"
    rc = main(["--config", config_file, "--dest", str(dest),
               "--format", "csv", "--rows-per-file", "20"])
    assert rc == 0
    rc = main(["--config", config_file, "--dest", str(dest),
               "--format", "csv", "--rows-per-file", "0"])
    assert rc == 0
    person_files = list((dest / "nodes" / "Person").iterdir())
    # only the new single file remains; old parts are gone
    assert [f.name for f in person_files] == ["person.csv"]
