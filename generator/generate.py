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
        props = [_prop(r["type"], p) for p in r.get("properties", [])]
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
    """Yield rows lazily: dicts keyed 'id' plus property names.

    Rows are generated one at a time so arbitrarily large counts never
    materialize in memory.
    """
    rng = random.Random(f"{seed}:{spec.label}")
    for i in range(spec.count):
        row = {"id": f"{spec.label}-{i}"}
        for prop in spec.properties:
            row[prop.name] = _value(rng, prop.type)
        yield row


def generate_rel_rows(spec, node_counts, seed):
    """Yield rows lazily. node_counts: {label: count} from the config's
    node specs."""
    rng = random.Random(f"{seed}:{spec.type}")
    start_count = node_counts[spec.start]
    end_count = node_counts[spec.end]
    for _ in range(spec.count):
        row = {
            "start": f"{spec.start}-{rng.randrange(start_count)}",
            "end": f"{spec.end}-{rng.randrange(end_count)}",
        }
        for prop in spec.properties:
            row[prop.name] = _value(rng, prop.type)
        yield row
