"""制造规则: 同层异网铜间距与分层禁铜区审查。"""
import json
import math
from itertools import product

from shapely.geometry import Polygon
from shapely.ops import nearest_points

from .netcheck import NetlistError, netcheck_report


class RuleError(NetlistError):
    """制造规则 JSON 校验错误。"""


def _reject_constant(value):
    raise RuleError("不允许 NaN 或 Infinity 数值")


def _finite(value, pos):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleError("必须为有限数字", pos)
    if not math.isfinite(value):
        raise RuleError("必须为有限数字", pos)
    return float(value)


def load_rules(raw, known_net_names):
    """解析铜间距覆盖值与禁铜多边形。"""
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except UnicodeDecodeError:
        raise RuleError("规则文件必须为 UTF-8 编码的 JSON")
    except json.JSONDecodeError as exc:
        raise RuleError(
            "规则 JSON 解析失败: %s" % exc,
            "line %d column %d" % (exc.lineno, exc.colno))
    if not isinstance(data, dict):
        raise RuleError("规则须为 JSON 对象")

    default = _finite(data.get("default_clearance_mm"),
                      "default_clearance_mm")
    if default <= 0:
        raise RuleError("default_clearance_mm 必须为正数",
                        "default_clearance_mm")

    overrides_raw = data.get("clearance_overrides", [])
    if not isinstance(overrides_raw, list):
        raise RuleError("clearance_overrides 必须为数组",
                        "clearance_overrides")
    overrides = {}
    known = set(known_net_names)
    for idx, item in enumerate(overrides_raw):
        pos = "clearance_overrides[%d]" % idx
        if not isinstance(item, dict):
            raise RuleError("覆盖规则必须为对象", pos)
        names = item.get("nets")
        if not isinstance(names, list) or len(names) != 2:
            raise RuleError("nets 必须包含两个网络名", pos + ".nets")
        if not all(isinstance(name, str) and name for name in names):
            raise RuleError("网络名必须为非空字符串", pos + ".nets")
        if names[0] == names[1]:
            raise RuleError("名称对必须是两个不同网络", pos + ".nets")
        for name in names:
            if name not in known:
                raise RuleError("未知网络名 %s" % name, pos + ".nets")
        value = _finite(item.get("clearance_mm"), pos + ".clearance_mm")
        if value <= 0:
            raise RuleError("clearance_mm 必须为正数", pos + ".clearance_mm")
        key = frozenset(names)
        if key in overrides:
            raise RuleError("重复的网络名称对 %s/%s" % tuple(sorted(key)), pos)
        overrides[key] = value

    keepouts_raw = data.get("keepouts", [])
    if not isinstance(keepouts_raw, list):
        raise RuleError("keepouts 必须为数组", "keepouts")
    keepouts = []
    seen_zone_ids = set()
    for idx, item in enumerate(keepouts_raw):
        pos = "keepouts[%d]" % idx
        if not isinstance(item, dict):
            raise RuleError("禁铜区必须为对象", pos)
        zone_id = item.get("id")
        if not isinstance(zone_id, str) or not zone_id:
            raise RuleError("id 必须为非空字符串", pos + ".id")
        if zone_id in seen_zone_ids:
            raise RuleError("重复的禁铜区 ID %s" % zone_id, pos + ".id")
        seen_zone_ids.add(zone_id)
        layer = item.get("layer")
        if layer not in ("top", "bottom"):
            raise RuleError("layer 必须为 top 或 bottom", pos + ".layer")
        polygon = item.get("polygon")
        if not isinstance(polygon, list) or len(polygon) < 3:
            raise RuleError("polygon 至少包含 3 个顶点", pos + ".polygon")
        points = []
        for pidx, point in enumerate(polygon):
            ppos = "%s.polygon[%d]" % (pos, pidx)
            if not isinstance(point, list) or len(point) != 2:
                raise RuleError("顶点必须为 [x, y]", ppos)
            points.append((_finite(point[0], ppos + "[0]"),
                           _finite(point[1], ppos + "[1]")))
        if points[0] == points[-1]:
            points = points[:-1]
        if len(points) < 3 or len(set(points)) < 3:
            raise RuleError("polygon 顶点不足或退化", pos + ".polygon")
        shape = Polygon(points)
        if not shape.is_valid or not shape.exterior.is_simple:
            raise RuleError("polygon 自交, 仅支持简单多边形",
                            pos + ".polygon")
        if shape.area <= 0:
            raise RuleError("polygon 面积不能为零", pos + ".polygon")
        keepouts.append({"id": zone_id, "layer": layer, "polygon": shape})

    return {"default": default, "overrides": overrides,
            "keepouts": keepouts}


def _point(point):
    return {"x": round(point.x, 6), "y": round(point.y, 6)}


def _threshold(rules, names_a, names_b):
    if not names_a or not names_b:
        return rules["default"], None
    values = [(rules["default"], None)]
    for name_a, name_b in product(names_a, names_b):
        key = frozenset((name_a, name_b))
        if key in rules["overrides"]:
            values.append((rules["overrides"][key],
                           tuple(sorted(key))))
    return max(values, key=lambda item: item[0])


def audit_rules(prepared, terminals, rules):
    """审查实体铜岛间距和禁铜接触, 并附同一份实际网络短断路报告。"""
    layers = prepared["layers"]
    comp_of = prepared["comp_of"]
    design_by_component = {}
    terminal_by_id = {terminal.id: terminal for terminal in terminals}
    for terminal_id, node in prepared["term_node"].items():
        cid = comp_of[node]
        design_by_component.setdefault(cid, set()).add(
            terminal_by_id[terminal_id].net)
    for cid in range(len(prepared["components"])):
        design_by_component.setdefault(cid, set())

    def island_info(layer, idx):
        node = ("island", layer, idx)
        cid = comp_of[node]
        return {
            "id": "%s#%d" % (layer, idx),
            "actual_network": cid,
            "design_nets": sorted(design_by_component[cid]),
        }

    clearance_violations = []
    for layer in ("top", "bottom"):
        islands = layers[layer]
        for idx_a in range(len(islands)):
            node_a = ("island", layer, idx_a)
            cid_a = comp_of[node_a]
            for idx_b in range(idx_a + 1, len(islands)):
                node_b = ("island", layer, idx_b)
                cid_b = comp_of[node_b]
                if cid_a == cid_b:
                    continue
                names_a = design_by_component[cid_a]
                names_b = design_by_component[cid_b]
                threshold, matched_pair = _threshold(rules, names_a, names_b)
                distance = islands[idx_a].distance(islands[idx_b])
                if distance < threshold:
                    near_a, near_b = nearest_points(
                        islands[idx_a], islands[idx_b])
                    clearance_violations.append({
                        "layer": layer,
                        "threshold_mm": round(threshold, 6),
                        "distance_mm": round(distance, 6),
                        "matched_net_pair": (list(matched_pair)
                                             if matched_pair else None),
                        "nearest_points": [_point(near_a), _point(near_b)],
                        "islands": [island_info(layer, idx_a),
                                    island_info(layer, idx_b)],
                    })

    keepout_violations = []
    for zone in rules["keepouts"]:
        layer = zone["layer"]
        for idx, island in enumerate(layers[layer]):
            contact = island.intersection(zone["polygon"])
            if not contact.is_empty:
                if getattr(contact, "area", 0.0) > 0:
                    near_island = contact.boundary.representative_point()
                else:
                    near_island = contact.representative_point()
                keepout_violations.append({
                    "keepout_id": zone["id"],
                    "layer": layer,
                    "island": island_info(layer, idx),
                    "contact_position": _point(near_island),
                })

    net_report = netcheck_report(prepared, terminals)
    manufacturing_ok = not clearance_violations and not keepout_violations
    report = {
        "clearance_violations": clearance_violations,
        "keepout_violations": keepout_violations,
        "manufacturing_ok": manufacturing_ok,
        "netcheck_ok": net_report["ok"],
        "ok": manufacturing_ok and net_report["ok"],
    }
    report.update(net_report)
    return report
