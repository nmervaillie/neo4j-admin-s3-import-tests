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
from generator.publish import clear_dest, publish
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
            paths = write_rows(rows, header, types,
                               stage / "nodes" / spec.label,
                               args.format, args.rows_per_file,
                               _slug(spec.label), spec.count)
            dest_dir = f"{base_dest}/nodes/{spec.label}"
            clear_dest(dest_dir)
            manifest["nodes"][spec.label] = publish(paths, dest_dir)
            manifest["rows"]["nodes"] += spec.count
        for spec in rels:
            rows = generate_rel_rows(spec, node_counts, args.seed)
            header, types = rel_columns(spec)
            paths = write_rows(rows, header, types,
                               stage / "relationships" / spec.type,
                               args.format, args.rows_per_file,
                               _slug(spec.type), spec.count)
            dest_dir = f"{base_dest}/relationships/{spec.type}"
            clear_dest(dest_dir)
            manifest["relationships"][spec.type] = publish(paths, dest_dir)
            manifest["rows"]["relationships"] += spec.count
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
