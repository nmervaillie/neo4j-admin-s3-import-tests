import json

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
    with pytest.raises(ValueError, match="uuid.*Person.name"):
        load_config(str(p))


def test_node_rows_count_ids_and_properties():
    spec = NodeSpec("Person", 100,
                    [Property("name", "string"), Property("age", "int")])
    rows = list(generate_node_rows(spec, seed=42))
    assert len(rows) == 100
    assert rows[0] == {"id": "Person-0", "name": rows[0]["name"], "age": rows[0]["age"]}
    assert all(r["id"].startswith("Person-") for r in rows)
    assert all(isinstance(r["age"], int) for r in rows)


def test_node_rows_unique_ids():
    rows = generate_node_rows(NodeSpec("Person", 500, []), seed=1)
    assert len({r["id"] for r in rows}) == 500


def test_seed_determinism():
    a = list(generate_node_rows(NodeSpec("Person", 50, [Property("v", "float")]), seed=7))
    b = list(generate_node_rows(NodeSpec("Person", 50, [Property("v", "float")]), seed=7))
    assert a == b
    c = list(generate_node_rows(NodeSpec("Person", 50, [Property("v", "float")]), seed=8))
    assert a != c


def test_rel_rows_reference_existing_ids():
    counts = {"Person": 100, "Company": 10}
    spec = RelSpec("WORKS_FOR", 90, "Person", "Company", [Property("since", "int")])
    rows = list(generate_rel_rows(spec, counts, seed=42))
    assert len(rows) == 90
    person_ids = {f"Person-{i}" for i in range(100)}
    company_ids = {f"Company-{i}" for i in range(10)}
    assert all(r["start"] in person_ids and r["end"] in company_ids for r in rows)
    assert all("since" in r for r in rows)


def test_generators_are_lazy():
    """Row generators must not materialize the full dataset."""
    spec = NodeSpec("Person", 3, [Property("v", "int")])
    gen = generate_node_rows(spec, seed=1)
    first = next(gen)
    assert first["id"] == "Person-0"  # value available before exhaustion
    assert len(list(gen)) == 2
