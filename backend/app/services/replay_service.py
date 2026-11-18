"""按业务日重放重建核销事实。

全局唯一策略（覆盖未钉事实）：
1. 已 sealed（钉扎）业务日一律拒绝重放，sealed 事实金额不改不删；
2. 单事务 BEGIN IMMEDIATE 内先作废该日全部 open 事实（status -> void，留存审计）；
3. 按事件湖中的 redeem 事件逐笔重建 open 事实；
4. 重建后在同一事务内与事件合计互证，不一致则整体回滚。

事件扫描、事实写、已钉闸、预览互证全部经由 repositories.facts 同源函数，
与 /redeem/preview、/redeem/seal 走同一套读取口径。
"""
from __future__ import annotations
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import settings as settings_repo
from app.repositories import facts as fact_repo


def replay_day(business_day: str | None = None) -> dict[str, Any]:
    conn = connect(immediate=True)  # 全局互斥：拿写锁后其他重放/钉扎/写事件排队
    try:
        day = business_day or settings_repo.get(conn, "business_day")
        if not day:
            raise HTTPException(400, "business_day 缺失")

        # 已钉闸：sealed 日拒绝重放，绝不改写已 sealed 事实金额
        if fact_repo.day_is_sealed(conn, day):
            raise HTTPException(409, f"业务日 {day} 已钉扎（sealed），拒绝重放")

        # 数据卫生闸：同码多笔 redeem 无法在 open 唯一约束下表达，先拦截不写库
        dup = fact_repo.duplicate_redeem_codes(conn, day)
        if dup:
            raise HTTPException(400, f"业务日 {day} 存在同券码多笔核销事件: {', '.join(dup)}")

        events = fact_repo.redeem_events_for_day(conn, day)

        # 覆盖未钉事实：作废旧 open（sealed/void 行不受影响），再按事件重建
        voided = fact_repo.void_open_facts_for_day(conn, day)
        created = 0
        for e in events:
            fact_repo.insert_fact(
                conn,
                coupon_code=e["coupon_code"],
                campaign_code=e["campaign_code"],
                amount=float(e["amount"]),
                business_day=day,
                event_id=int(e["id"]),
                status="open",
            )
            created += 1

        # 同源互证：重建结果必须与事件合计可对，否则回滚整个重放
        snapshot = fact_repo.build_day_snapshot(conn, day)
        if not snapshot["reconciled"]:
            conn.rollback()
            raise HTTPException(
                409,
                "重放互证失败：open 事实与 redeem 事件合计不一致，已回滚 "
                f"(open={snapshot['open_total']}, events={snapshot['redeem_event_total']})",
            )

        conn.commit()
        return {
            "business_day": day,
            "strategy": "overwrite_open",
            "voided_open_facts": voided,
            "created_open_facts": created,
            "reconciled": True,
            "preview": snapshot,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(409, f"重放重建失败，已回滚: {exc}") from exc
    finally:
        conn.close()
