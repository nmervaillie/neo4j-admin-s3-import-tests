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
        raise ValueError(
            f"malformed S3 destination: {dest!r}; expected s3://bucket/prefix")
    return bucket, prefix.rstrip("/")


def _s3_client():
    return boto3.client("s3")


def clear_dest(dest: str):
    """Remove existing files under dest (local dir or s3:// prefix).

    Called before publishing so regeneration never leaves stale files
    (e.g. old chunked parts after switching rows_per_file).
    """
    if is_s3_dest(dest):
        bucket, prefix = parse_s3(dest)
        client = _s3_client()
        paginator = client.get_paginator("list_objects_v2")
        keys = [o["Key"] for page in paginator.paginate(
                    Bucket=bucket, Prefix=f"{prefix}/")
                for o in page.get("Contents", [])]
        for i in range(0, len(keys), 1000):
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
        return
    dest_dir = Path(dest)
    if dest_dir.exists():
        for child in dest_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()


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
