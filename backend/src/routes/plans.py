"""票量计划的发布、查询与基于计划的票量凑批运行。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.models.identity import User
from src.models.jobs import QuotaPlan, Run
from src.models.source import Dataset
from src.schemas.requests import QuotaPlanRequest, QuotaRunRequest
from src.services.audit import record
from src.services.plans import active_plan, active_plan_map, plan_view, publish_plan
from src.utils import views
from src.utils.wakeup import wake

router = APIRouter(prefix='/quota-plans')


def require_dataset(db, dataset_id):
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(404, '找不到指定的数据批次')
    return dataset


@router.get('')
def index(dataset_id: int | None = None, db: Session = Depends(connection),
          actor: User = Depends(current)):
    query = select(QuotaPlan).order_by(QuotaPlan.version.desc()).limit(100)
    if dataset_id is not None:
        query = query.where(QuotaPlan.dataset_id == dataset_id)
    return [plan_view(item) for item in db.scalars(query)]


@router.get('/active')
def active(dataset_id: int, db: Session = Depends(connection),
           actor: User = Depends(current)):
    require_dataset(db, dataset_id)
    plan = active_plan(db, dataset_id)
    if plan is None:
        raise HTTPException(404, '该数据批次还没有生效的票量计划')
    return plan_view(plan)


@router.post('', status_code=201)
def publish(body: QuotaPlanRequest, db: Session = Depends(connection),
            actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    """发布新版本票量计划；更正与在途计算可并发，旧计划运行同步作废。"""
    dataset = require_dataset(db, body.dataset_id)
    plan, invalidated = publish_plan(db, dataset, body.quotas, actor.username)
    db.commit()
    wake()
    response = plan_view(plan)
    response['invalidated_runs'] = invalidated
    return response


@router.post('/{plan_id}/runs', status_code=202)
def start_run(plan_id: int, body: QuotaRunRequest, db: Session = Depends(connection),
              actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    """基于已发布计划发起票量凑批运行，运行快照计划版本与配对数据。"""
    plan = db.get(QuotaPlan, plan_id)
    if plan is None or plan.dataset_id != body.dataset_id:
        raise HTTPException(404, '找不到该票量计划或与数据批次不匹配')
    if plan.superseded:
        raise HTTPException(409, '该计划已被新版本更正，请基于最新计划发起运行')
    current = active_plan(db, body.dataset_id)
    if current is None or current.id != plan.id:
        raise HTTPException(409, '该计划已不是当前生效版本，请刷新后重试')
    item = Run(
        name=body.name, dataset_id=body.dataset_id, plan_id=plan.id,
        plan_version=plan.version, author=actor.username,
        params={'quotas': plan.quotas},
    )
    db.add(item)
    db.flush()
    record(db, actor.username, 'quota_run.queued', item.id,
           plan_id=plan.id, plan_version=plan.version, dataset_id=body.dataset_id)
    db.commit()
    wake()
    active_ids = active_plan_map(db, [item.dataset_id])
    return views.run(item, detail=True, active_plan_ids=active_ids)
