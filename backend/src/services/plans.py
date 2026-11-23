"""票量计划的版本管理与旧计划运行作废。"""

from sqlalchemy import func, select, update

from src.constants.enums import Status
from src.domain.quota import normalize_quotas
from src.models.jobs import QuotaPlan, Run
from src.services.audit import record

SUPERSEDED_MESSAGE = "票量计划已更正为新版本，旧计划结果不生效。"


def active_plan(db, dataset_id):
    """返回指定数据批次当前生效的计划（版本号最大且未作废）。"""
    return db.scalar(
        select(QuotaPlan)
        .where(QuotaPlan.dataset_id == dataset_id, QuotaPlan.superseded.is_(False))
        .order_by(QuotaPlan.version.desc())
        .limit(1)
    )


def active_plan_map(db, dataset_ids):
    """批量取多个数据批次的生效计划 id，供运行列表判定有效性。"""
    ids = [dataset_id for dataset_id in set(dataset_ids) if dataset_id is not None]
    if not ids:
        return {}
    plans = db.scalars(
        select(QuotaPlan)
        .where(QuotaPlan.dataset_id.in_(ids), QuotaPlan.superseded.is_(False))
        .order_by(QuotaPlan.version.desc())
    ).all()
    mapping = {}
    for plan in plans:
        # 已按版本倒序，首个出现的即该批次的生效计划。
        mapping.setdefault(plan.dataset_id, plan.id)
    return mapping


def run_effective(db, run):
    """运行结果是否仍然生效。

    统计验收运行（无计划）始终生效；票量凑批运行仅当其计划仍是当前生效版本时生效。
    页面展示与 CSV 下载共用同一判定，保证旧计划结果在任何入口都不生效。
    """
    if run.plan_id is None:
        return True
    plan = active_plan(db, run.dataset_id)
    return plan is not None and plan.id == run.plan_id


def _invalidate_inflight(db, plan_id):
    """把同一旧计划下仍在排队/计算的运行原子置为作废。

    正在执行的 worker 靠行锁与状态复核兜底：提交结果前若发现状态已被本函数
    改为 superseded，则放弃写入，旧计划结果不会落库、也不会生成可下载文件。
    """
    result = db.execute(
        update(Run)
        .where(Run.plan_id == plan_id,
               Run.status.in_((Status.QUEUED, Status.RUNNING)))
        .values(status=Status.SUPERSEDED, error=SUPERSEDED_MESSAGE, result=None,
                export_file=None, lease_until=None)
        .returning(Run.id)
    )
    return [row[0] for row in result.all()]


def publish_plan(db, dataset, quotas, actor):
    """校验并发布新版本票量计划，同时作废旧计划的在途运行。

    整个过程在调用方事务内完成，保证「更正计划」与「作废旧运行」对外原子可见。
    """
    normalized = normalize_quotas(quotas, dataset.rows)
    previous = active_plan(db, dataset.id)
    next_version = (db.scalar(select(func.coalesce(func.max(QuotaPlan.version), 0))) or 0) + 1
    plan = QuotaPlan(version=next_version, dataset_id=dataset.id,
                     quotas=normalized, superseded=False, author=actor)
    db.add(plan)
    db.flush()

    invalidated = []
    if previous is not None:
        previous.superseded = True
        invalidated = _invalidate_inflight(db, previous.id)
    record(db, actor, 'quota_plan.published', plan.id,
           version=next_version, dataset_id=dataset.id,
           superseded_plan=previous.id if previous else None,
           invalidated_runs=invalidated, quotas=normalized)
    return plan, invalidated


def plan_view(plan):
    return {
        'id': plan.id,
        'version': plan.version,
        'dataset_id': plan.dataset_id,
        'quotas': plan.quotas,
        'superseded': plan.superseded,
        'author': plan.author,
        'created_at': plan.created_at.isoformat(),
    }
