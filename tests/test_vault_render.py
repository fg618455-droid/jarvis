from __future__ import annotations

import re

import pytest
import yaml

from jarvis.memory.graph import MemoryNode
from jarvis.memory.vault.render import (
    END_MARKER,
    filename_for_node,
    render_node,
    split_protected_region,
)


pytestmark = pytest.mark.unit


def _node(**values) -> MemoryNode:
    base = {
        "id": "a1b2c3d4-e5f6-4789-a0b1-c2d3e4f5a6b7",
        "name": "Food preferences",
        "description": "What the user eats, likes, and avoids.",
        "data": "Regularly eats sushi\nDislikes coriander",
        "parent_id": "user",
        "access_count": 12,
        "created_at": "2026-08-11T10:00:00+02:00",
        "updated_at": "2026-08-11T14:03:00+02:00",
        "data_token_count": 340,
    }
    base.update(values)
    return MemoryNode(**base)


def _frontmatter(markdown: str) -> dict:
    match = re.match(r"\A---\n(.*?)\n---\n", markdown, re.DOTALL)
    assert match is not None
    return yaml.safe_load(match.group(1))


def test_frontmatter_round_trips_tricky_yaml_scalars():
    node = _node(
        id='#lead: "quoted"\nnext',
        created_at="2026-08-11T10:00:00+02:00",
        updated_at="2026-08-11T14:03:00+02:00",
    )

    parsed = _frontmatter(render_node(node, "user: private", "User"))

    assert parsed["jarvis_node_id"] == '#lead: "quoted"\nnext'
    assert parsed["jarvis_branch"] == "user: private"
    assert parsed["updated"] == "2026-08-11T14:03:00+02:00"
    assert parsed["jarvis_managed"] is True


def test_hostile_fact_text_cannot_create_wikilinks_or_frontmatter():
    markdown = render_node(
        _node(data="[[Some Note]]\n---\n# heading-like fact"),
        "user",
        "User",
    )

    assert "[[Some Note]]" not in markdown
    assert r"\[\[Some Note]]" in markdown
    assert "\n- ---\n" in markdown
    assert "\n- # heading-like fact\n" in markdown
    assert markdown.count("\n---\n") == 1


def test_filename_strips_forbidden_and_reserved_characters():
    filename = filename_for_node(
        _node(name='../../../# ^ [bad] | <name>: "x"?*'),
        "User",
    )

    assert filename.endswith("(a1b2c3d4).md")
    assert len(filename) < 120
    assert not any(char in filename for char in '#^[]|<>:"/\\?*')
    assert ".." not in filename


def test_empty_and_long_names_produce_legal_bounded_filenames():
    empty = filename_for_node(_node(name='<>:"/\\|?*#^[]'), "User")
    long_name = "word " * 100
    bounded = filename_for_node(_node(name=long_name), "A very long branch label " * 5)

    assert "Untitled" in empty
    assert len(bounded) < 120


def test_related_wikilinks_use_exact_generated_filenames():
    parent = _node(id="user", name="User", parent_id="root")
    node = _node()
    child = _node(
        id="d4e5f6a7-1111-2222-3333-444444444444",
        name="Restaurants",
        parent_id=node.id,
    )

    markdown = render_node(
        node,
        "user",
        "User",
        parent=parent,
        children=[child],
    )

    parent_stem = filename_for_node(parent, "User")[:-3]
    child_stem = filename_for_node(child, "User")[:-3]
    assert f"[[{parent_stem}|User]]" in markdown
    assert f"[[{child_stem}|Restaurants]]" in markdown


def test_fixed_branch_root_filename_matches_related_link_contract():
    branch = _node(id="user", name="User", parent_id="root")
    assert filename_for_node(branch, "User") == "User (user).md"


def test_protected_region_split_preserves_tail_verbatim():
    tail = "\n\nMy own text\n- [[Private link]]\n"
    machine, protected = split_protected_region("machine\n" + END_MARKER + tail)

    assert machine == "machine\n"
    assert protected == tail


def test_missing_marker_treats_whole_file_as_machine_content():
    machine, protected = split_protected_region("managed but markerless")
    assert machine == "managed but markerless"
    assert protected == ""
