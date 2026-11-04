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


@mock_aws
def test_publish_s3_uploads_many_files_preserving_order(tmp_path):
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.create_bucket(
        Bucket="test-bucket",
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    files = []
    for i in range(6):
        f = tmp_path / f"person-part-{i:05d}.csv"
        f.write_text(f"id:ID\nPerson-{i}\n")
        files.append(f)
    dests = publish(files, "s3://test-bucket/source/nodes/Person/")
    assert len(dests) == 6
    assert dests == [f"s3://test-bucket/source/nodes/Person/{f.name}"
                     for f in files]
    keys = {o["Key"] for o in s3.list_objects_v2(
        Bucket="test-bucket", Prefix="source/")["Contents"]}
    assert keys == {f"source/nodes/Person/{f.name}" for f in files}


def test_clear_dest_local_removes_old_files(tmp_path):
    from generator.publish import clear_dest
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "person.csv").write_text("stale")
    clear_dest(str(dest))
    assert dest.exists() and not list(dest.iterdir())


@mock_aws
def test_clear_dest_s3_deletes_old_objects():
    from generator.publish import clear_dest
    s3 = boto3.client("s3", region_name="eu-west-1")
    s3.create_bucket(
        Bucket="test-bucket",
        CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})
    s3.put_object(Bucket="test-bucket", Key="source/nodes/Person/person.csv",
                  Body=b"stale")
    s3.put_object(Bucket="test-bucket", Key="source/nodes/Company/company.csv",
                  Body=b"keep")
    clear_dest("s3://test-bucket/source/nodes/Person/")
    keys = {o["Key"] for o in s3.list_objects_v2(
        Bucket="test-bucket", Prefix="source/")["Contents"]}
    assert "source/nodes/Person/person.csv" not in keys
    assert "source/nodes/Company/company.csv" in keys
