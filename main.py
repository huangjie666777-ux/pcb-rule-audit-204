
"""FastAPI 请求处理层: 上传、铜层重建、SVG 下载、双层网表核对。"""
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from copper_drc204.errors import GerberError
from copper_drc204.geometry import build_geometry, summarize
from copper_drc204.netcheck import NetlistError, analyze, load_netlist
from copper_drc204.parser import Parser
from copper_drc204.svg_export import geometry_to_svg

app = FastAPI(title="Gerber Rule Audit 204")

# svg_id -> (svg_text, stats)
_results = {}


def _rebuild(text, tolerance):
    parser = Parser(text)
    events = parser.parse()
    geom = build_geometry(events, parser.apertures, tolerance)
    stats = summarize(geom)
    svg = geometry_to_svg(geom)
    return stats, svg, geom


@app.post("/api/rebuild")
async def rebuild(file: UploadFile = File(...),
                  tolerance: float = Form(0.01)):
    if tolerance <= 0:
        raise HTTPException(422, "tolerance 必须为正数(毫米)")
    raw = await file.read()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise HTTPException(422, "仅支持 ASCII 编码的 Gerber 文件")
    try:
        stats, svg, _geom = _rebuild(text, tolerance)
    except GerberError as exc:
        raise HTTPException(422, {
            "error": exc.message, "line": exc.line, "source": exc.source})
    svg_id = uuid.uuid4().hex
    _results[svg_id] = svg
    stats["svg_id"] = svg_id
    stats["svg_url"] = "/api/rebuild/%s/svg" % svg_id
    return stats


@app.get("/api/rebuild/{svg_id}/svg")
async def download_svg(svg_id: str):
    svg = _results.get(svg_id)
    if svg is None:
        raise HTTPException(404, "结果不存在或已过期")
    return Response(
        svg, media_type="image/svg+xml",
        headers={"Content-Disposition":
                 'attachment; filename="copper_%s.svg"' % svg_id[:8]})


@app.post("/api/netcheck")
async def netcheck(top: UploadFile = File(...),
                   bottom: UploadFile = File(...),
                   netlist: UploadFile = File(...),
                   tolerance: float = Form(0.01)):
    if tolerance <= 0:
        raise HTTPException(422, "tolerance 必须为正数(毫米)")
    geoms = {}
    for label, upload in (("顶层", top), ("底层", bottom)):
        raw = await upload.read()
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            raise HTTPException(422, "%s Gerber 仅支持 ASCII 编码" % label)
        try:
            parser = Parser(text)
            events = parser.parse()
            geoms[label] = build_geometry(events, parser.apertures, tolerance)
        except GerberError as exc:
            raise HTTPException(422, {
                "error": "%s Gerber: %s" % (label, exc.message),
                "line": exc.line, "source": exc.source})
    raw = await netlist.read()
    try:
        terminals, holes = load_netlist(raw)
    except NetlistError as exc:
        raise HTTPException(422, {
            "error": exc.message, "position": exc.position})
    return analyze(geoms["顶层"], geoms["底层"], terminals, holes, tolerance)
