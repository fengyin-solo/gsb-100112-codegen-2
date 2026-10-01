"""线段裁剪台接口。

导入与发布严格按「解析、分段核对、冲突裁定、正式发布」推进：
- /import            解析导入，裁出可通行线段，冲突区直接生成待裁切块；
- /check             分段核对（单条 / 全部），未核对的线段进不了发布；
- /conflicts/resolve 冲突裁定，败诉线段标为待裁剪；
- /advance           阶段推进，门禁不过会被拦下；
- /publish           正式发布，同版重算台账/巡查/工程清单；
- /resume            中断恢复，只补未发布线段，沿用原版本号。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.clip_workbench import service

router = APIRouter(prefix="/api/clip_workbench", tags=["线段裁剪台"])


class ImportPayload(BaseModel):
    file_name: str = "导入底图.csv"
    content: str = Field(default="", description="CSV 文本：路线,线段名称,起桩号,止桩号,历史区划名称")


class CheckPayload(BaseModel):
    segment_id: int | None = None


class ResolvePayload(BaseModel):
    conflict_id: int
    winner_id: int


class PublishPayload(BaseModel):
    limit: int | None = Field(default=None, description="本次最多发布几条，传小于可发布数会模拟发布中断")


@router.get("/state")
def get_state() -> dict[str, Any]:
    """裁剪台总览：草稿阶段、线段、待裁切块、版本、正式层统计、隔离清单。"""
    return service.state()


@router.get("/map")
def get_map() -> dict[str, Any]:
    """裁剪台地图摆位：正式层泳道、草稿线段、红色待裁切块。"""
    return service.map_view()


@router.post("/import", response_model=ActionResult)
def import_file(payload: ImportPayload) -> ActionResult:
    """解析导入文件：裁出可通行线段，与正式层重叠的部分按既有边界裁掉，
    批内重叠区生成待裁切块，重复线段直接去重。"""
    draft, error = service.import_file(payload.file_name, payload.content)
    if draft is None:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message="解析完成：可通行线段已裁出，请进入分段核对",
                        entry=draft)


@router.post("/check", response_model=ActionResult)
def check_segments(payload: CheckPayload) -> ActionResult:
    """分段核对：按正式桩号逐条核对；不传线段号则批量核对全部可核对线段。"""
    if payload.segment_id is None:
        count, message = service.check_all()
        return ActionResult(ok=True, message=message)
    ok, message = service.check_segment(payload.segment_id)
    return ActionResult(ok=ok, message=message)


@router.post("/conflicts/resolve", response_model=ActionResult)
def resolve_conflict(payload: ResolvePayload) -> ActionResult:
    """冲突裁定：在待裁切块里保留一条正式桩号线段，其余判离、标为待裁剪。"""
    ok, message = service.resolve_conflict(payload.conflict_id, payload.winner_id)
    return ActionResult(ok=ok, message=message)


@router.post("/advance", response_model=ActionResult)
def advance_stage() -> ActionResult:
    """进入下一阶段；前序核对没做完时直接拦下，不许跳级。"""
    ok, message = service.advance()
    return ActionResult(ok=ok, message=message)


@router.post("/publish", response_model=ActionResult)
def publish(payload: PublishPayload) -> ActionResult:
    """正式发布：只有完成全部前序核对、且不属于待裁切块败诉方的线段能发布；
    路段台账、巡查待办、工程清单按同一版本原子重算。"""
    result, message = service.publish(limit=payload.limit)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.post("/resume", response_model=ActionResult)
def resume() -> ActionResult:
    """中断恢复：沿用原版本号，只补发尚未发布的线段。"""
    result, message = service.resume()
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/layers")
def get_layers(version: str | None = None) -> dict[str, Any]:
    """读取某个发布版本锚定下的台账/巡查/工程清单与隔离清单。"""
    return service.layers(version)
