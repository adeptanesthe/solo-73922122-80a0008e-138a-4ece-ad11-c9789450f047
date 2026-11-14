"""野外调查观测更正汇交 API。

多台离线设备对同一观测的更正在此汇交：修订 DAG 乱序到达、待补齐自动重算、
多分支冲突、人工裁决、幂等重传、整包拒绝。双进程共库（SQLite WAL）。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, model_validator

from . import service
from .db import init_db, read_tx, write_tx


# ------------------------------------------------------------------ 模型

class PayloadFields(BaseModel):
    """修订载荷：物种+数量 或 删除标记，二者互斥。"""

    species: Optional[str] = Field(default=None, min_length=1, max_length=256)
    count: Optional[int] = Field(default=None, ge=0)
    delete: bool = False

    @model_validator(mode="after")
    def _check_payload(self):
        if self.delete:
            if self.species is not None or self.count is not None:
                raise ValueError("删除标记与物种/数量互斥，不能同时携带")
        else:
            if self.species is None or self.count is None:
                raise ValueError("非删除修订必须携带 species 与 count")
        return self


class RevisionIn(PayloadFields):
    observation_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(min_length=1, max_length=128)
    parents: list[str] = Field(default_factory=list)


class PackageIn(BaseModel):
    revisions: list[RevisionIn] = Field(min_length=1)


class ResolutionIn(PayloadFields):
    """人工裁决：parents 必须等于裁决时所见全部分支（当前 tips）。"""

    revision_id: str = Field(min_length=1, max_length=128)
    parents: list[str] = Field(default_factory=list)


class EffectiveValue(BaseModel):
    revision_id: str
    species: Optional[str]
    count: Optional[int]
    deleted: bool


class ObservationState(BaseModel):
    observation_id: str
    status: str = Field(description="pending=待补齐 / valid=有效 / conflict=冲突")
    tips: list[str] = Field(description="当前分支（无后继的修订）")
    missing_ancestors: list[str] = Field(description="未齐祖先（待补齐的修订号）")
    effective: Optional[EffectiveValue] = Field(description="有效值；待补齐或冲突时为 null")
    version: int


class PackageResult(BaseModel):
    accepted: list[str] = Field(description="本次新入库的修订号")
    duplicates: list[str] = Field(description="同号同内容幂等跳过的修订号")
    observations: list[ObservationState] = Field(description="受影响观测重算后的状态")


class RevisionOut(BaseModel):
    revision_id: str
    observation_id: str
    parents: list[str]
    species: Optional[str]
    count: Optional[int]
    delete: bool
    received_at: str


class ChangeRecord(BaseModel):
    """一条观测变更：结论变化当时在同一事务中落库的完整快照（不可变）。"""

    seq: int = Field(description="变更序号：提交次序，单调递增")
    observation_id: str
    status: str = Field(description="当时结论：pending=待补齐 / valid=有效 / conflict=冲突")
    tips: list[str] = Field(description="当时当前分支（无后继的修订）")
    missing_ancestors: list[str] = Field(description="当时未齐祖先（待补齐的修订号）")
    effective: Optional[EffectiveValue] = Field(
        description="当时有效值（含删除标记 deleted）；待补齐或冲突时为 null"
    )
    version: int = Field(description="当时观测状态版本")
    recorded_at: str = Field(description="记录时间（UTC）")


class ChangePage(BaseModel):
    changes: list[ChangeRecord] = Field(description="本页变更，按 seq 升序")
    next_cursor: int = Field(
        description="下一页游标：等于本页最后一条记录的 seq（空页保持原游标），"
        "只会越过已返回的记录"
    )


# ------------------------------------------------------------------ 应用

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="野外调查观测更正汇交 API",
    version="1.1.0",
    description=(
        "汇合多台离线设备对同一观测的更正：修订按 DAG 组织，允许子先父后乱序到达；"
        "祖先未齐标 **pending**（不出有效值），补齐自动重算；单分支为 **valid**；"
        "多分支（含改值与删除并存）为 **conflict**，需人工裁决。"
        "裁决以所见全部分支为父，提交时分支已变返回 409；"
        "同号同内容重传幂等，异内容/循环/跨观测引用整包拒绝。"
        "**观测变更流**：自启用起，上报或裁决改变观测结论（状态/当前分支/未齐祖先/有效值）"
        "即在同一事务中按提交次序记录当时的完整结论（含删除标记），"
        "拒绝与纯重传不产生记录；`GET /changes` 按序号游标限量领取，"
        "下一游标只越过已返回的记录，不重读、不漏读，分析库可据此撤回失效值。"
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/docs")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


def _to_payload(r: RevisionIn) -> service.RevisionPayload:
    return service.RevisionPayload(
        observation_id=r.observation_id,
        revision_id=r.revision_id,
        parents=r.parents,
        species=r.species,
        count=r.count,
        delete=r.delete,
    )


@app.post("/packages", response_model=PackageResult, tags=["汇交"])
def submit_package(pkg: PackageIn):
    """上报一个 JSON 包（可含多条修订、多个观测）。整体原子：任一非法则整包拒绝。"""
    try:
        with write_tx() as conn:
            return service.submit_package(conn, [_to_payload(r) for r in pkg.revisions])
    except service.PackageRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/observations", response_model=list[ObservationState], tags=["查询"])
def list_observations(
    status: Optional[str] = Query(default=None, pattern="^(pending|valid|conflict)$")
):
    """列出全部观测状态，可按 status 过滤。"""
    with read_tx() as conn:
        return service.list_observations(conn, status)


@app.get("/conflicts", response_model=list[ObservationState], tags=["查询"])
def list_conflicts():
    """查冲突：返回全部多分支观测及其当前分支（tips），供裁决取数。"""
    with read_tx() as conn:
        return service.list_observations(conn, "conflict")


@app.get("/observations/{observation_id}", response_model=ObservationState, tags=["查询"])
def get_observation(observation_id: str):
    """查单个观测：状态、当前分支、未齐祖先、有效值。"""
    with read_tx() as conn:
        state = service.get_observation(conn, observation_id)
    if state is None:
        raise HTTPException(status_code=404, detail="观测不存在")
    return state


@app.get(
    "/observations/{observation_id}/revisions",
    response_model=list[RevisionOut],
    tags=["查询"],
)
def get_observation_revisions(observation_id: str):
    """查观测的全部修订（DAG 节点）。"""
    with read_tx() as conn:
        state = service.get_observation(conn, observation_id)
        if state is None:
            raise HTTPException(status_code=404, detail="观测不存在")
        return service.list_revisions(conn, observation_id)


@app.get("/changes", response_model=ChangePage, tags=["变更流"])
def get_changes(
    since: int = Query(default=0, ge=0, description="序号游标：仅返回 seq 大于此值的变更"),
    limit: int = Query(default=100, ge=1, le=1000, description="单页最大条数"),
):
    """领取观测变更流。

    自启用起，每次上报/裁决改变观测结论都在同一 SQLite 事务中按提交次序
    记录当时的完整结论（状态、当前分支、未齐祖先、有效值，含删除标记）；
    被拒绝的请求与纯重传不产生记录。记录不可变：翻页、双进程交错写入或
    重启后，旧记录仍保留原结论。用响应中的 `next_cursor` 作为下一次的
    `since` 即可不重读、不漏读；当有效值变化、转为冲突/待补齐或被删除时，
    分析库可据此撤回已同步的失效值。
    """
    with read_tx() as conn:
        return service.list_changes(conn, since, limit)


@app.post(
    "/observations/{observation_id}/resolutions",
    response_model=ObservationState,
    status_code=201,
    tags=["裁决"],
)
def resolve_observation(observation_id: str, body: ResolutionIn, response: Response):
    """人工裁决：以所见全部分支为父新建修订。分支已变返回 409 及当前分支。"""
    rev = service.RevisionPayload(
        observation_id=observation_id,
        revision_id=body.revision_id,
        parents=body.parents,
        species=body.species,
        count=body.count,
        delete=body.delete,
    )
    try:
        with write_tx() as conn:
            state, replayed = service.resolve_observation(conn, observation_id, rev)
    except service.ObservationNotFound:
        raise HTTPException(status_code=404, detail="观测不存在")
    except service.BranchesChanged as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "提交时分支已变化，请按当前分支重新裁决",
                "current_tips": exc.current_tips,
                "status": exc.status,
            },
        )
    except service.PackageRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if replayed:  # 同号同内容重传：幂等返回
        response.status_code = 200
    return state
