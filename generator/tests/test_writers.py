import csv

import pyarrow.parquet as pq
import pytest

from generator.generate import Property, NodeSpec, RelSpec
from generator.generate import generate_node_rows
from generator.writers import node_columns, rel_columns, write_rows

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
    for j, p in enumerate(paths):
        with open(p, newline="") as f:
            lines = sum(1 for _ in csv.reader(f))
        total += lines - (1 if j == 0 else 0)  # header only in first part
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
    with pytest.raises(ValueError, match="unknown format"):
        write_rows(rows, header, types, tmp_path, "orc", 0, "person")


def test_chunked_csv_header_only_in_first_part(tmp_path):
    """neo4j-admin treats comma-joined files as one logical file:
    only the first part carries the header row."""
    rows = generate_node_rows(NODE, seed=1)
    header, types = node_columns(NODE)
    paths = write_rows(rows, header, types, tmp_path, "csv", 100, "person")
    assert len(paths) == 3
    with open(paths[0], newline="") as f:
        first = list(csv.reader(f))
    assert first[0] == header
    assert len(first) == 101  # header + 100 rows
    for p in paths[1:]:
        with open(p, newline="") as f:
            rest = list(csv.reader(f))
        assert rest[0] != header  # no header in later parts
        assert all(len(r) == len(header) for r in rest)
