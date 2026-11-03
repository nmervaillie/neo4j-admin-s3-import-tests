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
    single = rows_per_file <= 0 or rows_per_file >= len(rows)
    paths = []
    for i, chunk in enumerate(_chunks(rows, rows_per_file), start=1):
        name = f"{base_name}.{ext}" if single else f"{base_name}-part-{i:05d}.{ext}"
        path = out_dir / name
        if fmt == "csv":
            _write_csv(chunk, header, keys, path)
        else:
            _write_parquet(chunk, header, keys, gen_types, path)
        paths.append(path)
    return paths
