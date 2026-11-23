import logging
from datetime import date, timedelta
from sqlalchemy import and_, or_, select
from src.config.database import Session
from src.constants.enums import Status
from src.domain.analysis import analyze
from src.domain.quota import select_quota
from src.domain.spec import SPEC
from src.models.jobs import Delivery, Run, Schedule
from src.models.source import Dataset
from src.services.audit import record
from src.services.plans import SUPERSEDED_MESSAGE, active_plan
from src.utils.exports import write_report
from src.utils.time import now

log = logging.getLogger(__name__)


def materialize_due():
    if not SPEC.get('scheduled'):
        return
    stamp = now()
    with Session.begin() as db:
        schedules = db.scalars(select(Schedule).where(Schedule.enabled.is_(True), Schedule.next_at <= stamp)
                                .order_by(Schedule.next_at).limit(20).with_for_update(skip_locked=True))
        for item in schedules:
            due = item.next_at
            dataset_id = item.dataset_id
            params = dict(item.params)
            if item.repeat_daily:
                elapsed_days = (stamp - due).days
                due += timedelta(days=elapsed_days)
                period_key = SPEC['schedule_date_param']
                report_day = date.fromisoformat(params[period_key]) + timedelta(days=elapsed_days)
                params[period_key] = report_day.isoformat()
                item.params = {**params, period_key: (report_day + timedelta(days=1)).isoformat()}
                dataset_id = db.scalar(select(Dataset.id).order_by(Dataset.id.desc()).limit(1)) or dataset_id
                item.next_at = due + timedelta(days=1)
            else:
                item.enabled = False
            db.add(Run(dataset_id=dataset_id, schedule_id=item.id, scheduled_at=due,
                       name=item.name, params=params, author=item.author))


def claim():
    stamp = now()
    with Session.begin() as db:
        query = select(Run).where(or_(Run.status == Status.QUEUED,
                                      and_(Run.status == Status.RUNNING, Run.lease_until < stamp)))
        job = db.scalar(query.order_by(Run.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            return None
        # 租约领取瞬间再核对一次计划版本，避免作废后仍被重跑拉起。
        if job.plan_id is not None:
            plan = active_plan(db, job.dataset_id)
            if plan is None or plan.id != job.plan_id:
                job.status = Status.SUPERSEDED
                job.error = SUPERSEDED_MESSAGE
                job.lease_until = None
                record(db, 'worker', 'analysis.superseded', job.id, reason=SUPERSEDED_MESSAGE)
                return None
        if job.attempt >= 3:
            job.status = Status.FAILED
            job.error = '后台进程多次中断，请发起新的运行'
            job.finished_at = stamp
            record(db, 'worker', 'analysis.failed', job.id, reason=job.error)
            return None
        job.status = Status.RUNNING
        job.attempt += 1
        job.lease_until = stamp + timedelta(minutes=3)
        records = db.get(Dataset, job.dataset_id).rows
        return job.id, job.attempt, records, job.params, job.plan_id


def execute(lease):
    job_id, attempt, rows, params, plan_id = lease
    result = failure = None
    try:
        # 票量凑批运行携带 plan_id 与 quotas；统计验收运行走原有 McNemar 口径。
        if plan_id is not None:
            result = select_quota(rows, params)
        else:
            result = analyze(rows, params)
    except ValueError as error:
        failure = str(error)[:500]
    except Exception:
        log.exception('Analysis %s failed', job_id)
        failure = '计算失败，请检查字段口径后重试'
    with Session.begin() as db:
        job = db.get(Run, job_id, with_for_update=True)
        if job.attempt != attempt or job.status != Status.RUNNING:
            return
        # 计算期间计划被更正：本结果属于旧计划，一律丢弃，不写文件、不产生可下载结果。
        if job.plan_id is not None:
            plan = active_plan(db, job.dataset_id)
            if plan is None or plan.id != job.plan_id:
                job.status = Status.SUPERSEDED
                job.error = SUPERSEDED_MESSAGE
                job.result = None
                job.export_file = None
                job.lease_until = None
                job.finished_at = now()
                record(db, 'worker', 'analysis.superseded', job.id, reason=SUPERSEDED_MESSAGE)
                return
        # 通过版本复核后才落盘，保证页面与下载来自同一份生效结果。
        export = write_report(job_id, attempt, result) if not failure else None
        job.lease_until = None
        job.finished_at = now()
        job.status = Status.FAILED if failure else Status.COMPLETE
        job.result = None if failure else result
        job.export_file = export
        job.error = failure
        record(db, 'worker', f'analysis.{job.status}', job.id, attempt=attempt, reason=failure)
        if job.schedule_id:
            plan = db.get(Schedule, job.schedule_id)
            ready = not failure and result.get('ready', True)
            detail = '已保存 CSV 到站内收件箱' if ready else (failure or result['headline'])
            receipt = Delivery(run_id=job.id, recipient=plan.recipient, state='delivered' if ready else 'blocked', detail=detail[:500])
            db.add(receipt)
            record(db, 'worker', f'delivery.{receipt.state}', job.id, recipient=plan.recipient)
