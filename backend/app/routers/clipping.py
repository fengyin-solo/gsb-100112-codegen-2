"""线段裁剪台接口：导入解析 → 分段核对 → 冲突裁定 → 正式发布。

四阶段由服务端状态机强约束，任何跳级请求都会被拦下；
发布只产生同一版本的台账/巡查待办/工程边界，不允许只更新底图。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.services import clipping

router = APIRouter(prefix="/api/clipping", tags=["线段裁剪台"])


class CorrectPayload(BaseModel):
    candidate_id: int
    start_stake: str = Field(description="正式起点桩号，如 K0+800")
    end_stake: str = Field(description="正式终点桩号，如 K2+500")


class PiecePayload(BaseModel):
    checked: bool = True


class DecidePayload(BaseModel):
    decision: str = Field(description="keep_existing 保留既有边界 / use_formal 采用正式桩号裁入")


class PublishPayload(BaseModel):
    fail_after: int | None = Field(
        default=None, description="演示用：处理 N 段后模拟发布中断，留下裁剪草稿"
    )


def _raise(exc: clipping.ClippingError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.get("/state")
def get_state() -> dict[str, Any]:
    """裁剪台总览：当前版本、草稿阶段、台账/待办/工程边界重算结果。"""
    return clipping.state()


@router.get("/map")
def get_map() -> dict[str, Any]:
    """底图：路线骨架、正式路段与草稿切块（含 SVG 折线坐标）。"""
    return clipping.map_view()


@router.post("/import")
async def import_lines(file: UploadFile = File(...)) -> dict[str, Any]:
    """阶段一：上传线段文件并即时裁剪，冲突区直接成为待裁切块。"""
    raw = await file.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="文件需为 UTF-8 文本（JSON 或 CSV）") from exc
    try:
        return clipping.import_file(file.filename or "导入线段", content)
    except clipping.ClippingError as exc:
        raise _raise(exc) from exc


@router.post("/import/sample")
def import_sample() -> dict[str, Any]:
    """无文件时用内置样例走通四阶段，方便演示与验收。"""
    sample = (
        "路线,起点桩号,终点桩号,路段名称,历史区划,道路等级,车道数,路面类型,管养单位\n"
        "G104,K1+200,K3+000,G104北延段,老北关区,主干路,6,沥青混凝土,市政一处\n"
        "S205,K1+200,K2+200,S205东延段,旧东郊公社,次干路,4,水泥混凝土,市政三处"
    )
    try:
        return clipping.import_file("样例线段.csv", sample)
    except clipping.ClippingError as exc:
        raise _raise(exc) from exc


@router.post("/candidates/{candidate_id}/correct")
def correct_candidate(candidate_id: int, payload: CorrectPayload) -> dict[str, Any]:
    """阶段二：核对发现桩号有误，按正式桩号纠正并重新裁剪。"""
    try:
        return clipping.correct_candidate(
            candidate_id, payload.start_stake, payload.end_stake
        )
    except clipping.ClippingError as exc:
        raise _raise(exc) from exc
    except clipping.geometry.StakeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pieces/{piece_id}/check")
def check_piece(piece_id: int, payload: PiecePayload) -> dict[str, Any]:
    """阶段二：逐段确认可通行线段；未核对的线段不允许往后走。"""
    try:
        return clipping.check_piece(piece_id, payload.checked)
    except clipping.ClippingError as exc:
        raise _raise(exc) from exc


@router.post("/pieces/check-all")
def check_all_pieces() -> dict[str, Any]:
    """阶段二：一键核对全部可通行线段（冲突切块仍须逐块裁定）。"""
    try:
        return clipping.check_all()
    except clipping.ClippingError as exc:
        raise _raise(exc) from exc


@router.post("/pieces/{piece_id}/decide")
def decide_piece(piece_id: int, payload: DecidePayload) -> dict[str, Any]:
    """阶段三：冲突裁定。正式桩号优先；保留既有边界时原线不动。"""
    try:
        return clipping.decide_conflict(piece_id, payload.decision)
    except clipping.ClippingError as exc:
        raise _raise(exc) from exc


@router.post("/publish")
def publish(payload: PublishPayload) -> dict[str, Any]:
    """阶段四：正式发布，台账/待办/工程边界按同一版本重算。"""
    try:
        return clipping.publish(fail_after=payload.fail_after)
    except clipping.ClippingError as exc:
        raise _raise(exc) from exc


@router.post("/resume")
def resume_publish() -> dict[str, Any]:
    """发布中断后恢复：只补未发布线段，已入正式层的线段直接跳过。"""
    try:
        return clipping.publish()
    except clipping.ClippingError as exc:
        raise _raise(exc) from exc


@router.post("/discard")
def discard_draft() -> dict[str, Any]:
    """废弃裁剪草稿，撤出中断时已落入的线段，正式层恢复原样。"""
    try:
        return clipping.discard_draft()
    except clipping.ClippingError as exc:
        raise _raise(exc) from exc
