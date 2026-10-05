import json

import pytest
from fastapi.testclient import TestClient

from main import app
from copper_drc204.geometry import build_geometry
from copper_drc204.netcheck import Hole, Terminal, prepare_board
from copper_drc204.parser import Parser
from copper_drc204.rules import RuleError, audit_rules, load_rules

HEADER = "%FSLAX24Y24*%\n%MOMM*%\n"


def geom_of(body, tol=0.01):
    parser = Parser(HEADER + body + "\nM02*\n")
    return build_geometry(parser.parse(), parser.apertures, tol)


def square(x2, y2, x1=0, y1=0):
    return ("G36*\nX%dY%dD02*\nX%dY%dD01*\nX%dY%dD01*\n"
            "X%dY%dD01*\nX%dY%dD01*\nG37*"
            % (x1, y1, x2, y1, x2, y2, x1, y2, x1, y1))


def terminal(tid, net, layer, x, y):
    return Terminal(tid, net, layer, x, y, "terminals")


def rules(default=0.5, overrides=None, keepouts=None):
    return load_rules(json.dumps({
        "default_clearance_mm": default,
        "clearance_overrides": overrides or [],
        "keepouts": keepouts or [],
    }).encode(), {"A", "B", "C"})


def test_clearance_unordered_override_and_equal_distance_allowed():
    top = geom_of(square(100000, 100000) + "\n" +
                  square(200000, 100000, 110000, 0))
    bottom = geom_of("")
    terminals = [terminal("T1", "A", "top", 1, 5),
                 terminal("T2", "B", "top", 19, 5)]
    config = rules(0.5, [{"nets": ["B", "A"], "clearance_mm": 1.0}])
    prepared = prepare_board(top, bottom, terminals, [], 0.01)
    report = audit_rules(prepared, terminals, config)
    assert report["clearance_violations"] == []
    assert report["manufacturing_ok"] is True


def test_clearance_less_than_threshold_reports_solid_nearest_points():
    top = geom_of(square(100000, 100000) + "\n" +
                  square(200000, 100000, 109000, 0))
    bottom = geom_of("")
    terminals = [terminal("T1", "A", "top", 1, 5),
                 terminal("T2", "B", "top", 19, 5)]
    config = rules(1.0)
    prepared = prepare_board(top, bottom, terminals, [], 0.01)
    violations = audit_rules(prepared, terminals, config)["clearance_violations"]
    assert len(violations) == 1
    violation = violations[0]
    assert violation["layer"] == "top"
    assert violation["distance_mm"] == pytest.approx(0.9, abs=1e-6)
    assert violation["threshold_mm"] == 1.0
    point_xs = sorted(point["x"] for point in violation["nearest_points"])
    assert point_xs == pytest.approx([10.0, 10.9], abs=1e-6)
    design_names = sorted(
        name for island in violation["islands"] for name in island["design_nets"])
    assert design_names == ["A", "B"]


def test_multi_name_network_uses_max_applicable_threshold():
    top = geom_of(square(100000, 100000) + "\n" +
                  square(200000, 100000, 118000, 0))
    bottom = geom_of(square(200000, 100000))
    terminals = [
        terminal("T1", "A", "top", 1, 5),
        terminal("T2", "B", "bottom", 19, 5),
        terminal("T3", "C", "bottom", 19, 5),
    ]
    holes = [Hole("H1", 15, 5, 1.0, True, "holes")]
    config = rules(0.5, [
        {"nets": ["A", "C"], "clearance_mm": 2.0},
        {"nets": ["B", "C"], "clearance_mm": 0.8},
    ])
    prepared = prepare_board(top, bottom, terminals, holes, 0.01)
    violations = audit_rules(prepared, terminals, config)["clearance_violations"]
    assert len(violations) == 1
    assert violations[0]["threshold_mm"] == 2.0
    assert violations[0]["matched_net_pair"] == ["A", "C"]


def test_same_actual_network_not_checked_even_with_different_layer_overlap():
    top = geom_of(square(200000, 100000))
    bottom = geom_of(square(200000, 100000))
    terminals = [terminal("T1", "A", "top", 1, 5),
                 terminal("T2", "B", "bottom", 1, 5)]
    holes = [Hole("H1", 10, 5, 1.0, True, "holes")]
    config = rules(10.0)
    prepared = prepare_board(top, bottom, terminals, holes, 0.01)
    report = audit_rules(prepared, terminals, config)
    assert report["clearance_violations"] == []


def test_floating_island_keepout_contact_and_zone_inside_hole_allowed():
    top = geom_of(square(200000, 100000))
    bottom = geom_of("")
    keepouts = [
        {"id": "IN_HOLE", "layer": "top",
         "polygon": [[4.6, 4.6], [5.4, 4.6], [5.4, 5.4], [4.6, 5.4]]},
        {"id": "TOUCH", "layer": "top",
         "polygon": [[-0.5, 4.0], [0.5, 4.0], [0.5, 6.0], [-0.5, 6.0]]},
    ]
    config = rules(0.5, keepouts=keepouts)
    holes = [Hole("H1", 5, 5, 2.0, False, "holes")]
    prepared = prepare_board(top, bottom, [], holes, 0.01)
    violations = audit_rules(prepared, [], config)["keepout_violations"]
    assert [item["keepout_id"] for item in violations] == ["TOUCH"]
    assert violations[0]["contact_position"]["x"] == pytest.approx(0.5)
    assert violations[0]["island"]["design_nets"] == []


@pytest.mark.parametrize("rule", [
    {"default_clearance_mm": 0},
    {"default_clearance_mm": float("nan")},
])
def test_invalid_base_threshold_rejected(rule):
    with pytest.raises(RuleError):
        load_rules(json.dumps(rule).encode(), set())


def test_duplicate_pair_unknown_net_and_non_positive_override_rejected():
    base = {"default_clearance_mm": 0.5, "clearance_overrides": [
        {"nets": ["A", "B"], "clearance_mm": 1.0},
        {"nets": ["B", "A"], "clearance_mm": 2.0}]}
    with pytest.raises(RuleError, match="重复"):
        load_rules(json.dumps(base).encode(), {"A", "B"})
    base["clearance_overrides"] = [
        {"nets": ["A", "MISSING"], "clearance_mm": 1.0}]
    with pytest.raises(RuleError, match="未知"):
        load_rules(json.dumps(base).encode(), {"A", "B"})
    base["clearance_overrides"] = [
        {"nets": ["A", "B"], "clearance_mm": -1.0}]
    with pytest.raises(RuleError):
        load_rules(json.dumps(base).encode(), {"A", "B"})


def test_self_intersecting_and_zero_area_keepout_rejected():
    with pytest.raises(RuleError, match="自交"):
        rules(keepouts=[{"id": "K", "layer": "top", "polygon": [
            [0, 0], [2, 2], [2, 0], [0, 2]]}])
    with pytest.raises(RuleError, match="自交"):
        rules(keepouts=[{"id": "K2", "layer": "top", "polygon": [
            [0, 0], [1, 1], [2, 2]]}])


def test_drc_endpoint_rejects_ambiguous_terminal_as_422(tmp_path):
    def write(name, content):
        path = tmp_path / name
        path.write_text(content)
        return path

    top = write("top.gbr", HEADER + square(100000, 100000) + "\n" +
                square(200000, 200000, 100000, 100000) + "\nM02*\n")
    empty = write("bottom.gbr", HEADER + "M02*\n")
    netlist = write("net.json", json.dumps({
        "terminals": [{"id": "T1", "net": "A", "layer": "top",
                       "x": 10, "y": 10}],
        "holes": []}))
    rule_file = write("rules.json", json.dumps({
        "default_clearance_mm": 0.5, "clearance_overrides": [],
        "keepouts": []}))
    client = TestClient(app)
    with top.open("rb") as tf, empty.open("rb") as bf, \
            netlist.open("rb") as nf, rule_file.open("rb") as rf:
        response = client.post("/api/drc", files={
            "top": tf, "bottom": bf, "netlist": nf, "rules": rf})
    assert response.status_code == 422
    assert "歧义" in response.json()["detail"]["error"]
