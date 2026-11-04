"""File writers producing neo4j-admin import compatible CSV and Parquet."""

import csv
import gzip
import sys
from datetime import datetime
from itertools import islice
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


def _csv_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# rows buffered per parquet row group when writing a single (unchunked) file
PARQUET_ROW_GROUP = 100_000

# gzip level for CSV output: 6 writes ~2x faster than 9 at ~1% worse ratio
GZIP_COMPRESSLEVEL = 6


def _write_csv(rows, header, keys, path: Path, write_header: bool = True):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt", newline="",
                   compresslevel=GZIP_COMPRESSLEVEL) as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)
        for row in rows:
            writer.writerow([_csv_value(row[k]) for k in keys])
    tmp.rename(path)


def _write_parquet(rows, header, keys, gen_types, path: Path,
                   row_group_rows: int = 0):
    """Write rows to one parquet file, buffering at most row_group_rows
    rows in memory (0 = all rows, for already-bounded chunk inputs)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    schema = pa.schema(
        [(h, ARROW_TYPES[t]) for h, t in zip(header, gen_types)])
    rows = iter(rows)  # islice must see one shared iterator, not re-iterate
    with pq.ParquetWriter(tmp, schema) as writer:
        if row_group_rows <= 0:
            row_group_rows = sys.maxsize  # effectively unbounded
        while True:
            chunk = list(islice(rows, row_group_rows))
            if not chunk:
                break
            data = {h: pa.array([row[k] for row in chunk], type=ARROW_TYPES[t])
                    for h, k, t in zip(header, keys, gen_types)}
            writer.write_table(pa.table(data))
    tmp.rename(path)


def write_rows(rows, header, gen_types, out_dir, fmt, rows_per_file, base_name,
               total):
    """Write a row iterable to one or more files. Returns list of final
    file paths.

    Rows are consumed lazily; only rows_per_file rows are buffered at a
    time. total (expected row count) decides single-file vs chunked
    naming up front, before any row is consumed.

    Files are written with a .tmp suffix and renamed only when complete,
    so consumers never observe half-written files.
    """
    if fmt not in ("csv", "parquet"):
        raise ValueError(f"unknown format: {fmt}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = _keys(header)
    ext = "csv.gz" if fmt == "csv" else fmt
    single = rows_per_file <= 0 or rows_per_file >= total
    if single:
        path = out_dir / f"{base_name}.{ext}"
        if fmt == "csv":
            _write_csv(rows, header, keys, path)
        else:
            _write_parquet(rows, header, keys, gen_types, path,
                           row_group_rows=PARQUET_ROW_GROUP)
        return [path]
    paths = []
    i = 0
    rows = iter(rows)
    while True:
        chunk = list(islice(rows, rows_per_file))
        if not chunk:
            break
        i += 1
        path = out_dir / f"{base_name}-part-{i:05d}.{ext}"
        if fmt == "csv":
            # comma-joined chunked files are one logical input for
            # neo4j-admin: only the first part carries the header row
            _write_csv(chunk, header, keys, path, write_header=(i == 1))
        else:
            _write_parquet(chunk, header, keys, gen_types, path)
        paths.append(path)
    return paths
