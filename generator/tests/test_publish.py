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
