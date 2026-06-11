#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Automated running workflow with local school-data fallback."""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from database import get_db, init_db
from models import TsnAccount_Model
from tsnClient import tsnPasswordAuthServer
from tsnRunServer import TsnRunServer, TsnRunType

MAX_DISTANCE = 10.0


async def get_school_info_from_api(school_code: str):
    """Fetch school metadata from the upstream API."""
    import httpx

    url = f"https://h.tsnkj.com/upms/sysSchool/getSchoolInfo?schoolCode={school_code}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"User-Agent": "okhttp/4.9.0"})
            if resp.status_code != 200:
                logger.error(f"API 返回非 200 状态码: {resp.status_code}")
                return None

            data = resp.json()
            if data.get("code") not in (200, 0):
                logger.error(f"API 返回错误: {data.get('msg', '未知错误')}")
                return None

            school_data = data.get("data")
            if not school_data:
                logger.error("API 返回的 data 字段为空")
                return None

            return school_data
    except httpx.TimeoutException:
        logger.error("请求超时")
        return None
    except Exception as e:
        logger.error(f"请求异常: {e}")
        return None


def get_school_info_from_local_file(school_id: int, school_code: Optional[str] = None):
    """Load school metadata from the bundled school_data.json file."""
    school_file = Path(__file__).with_name("school_data.json")
    if not school_file.exists():
        return None

    try:
        with school_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"读取本地学校数据失败: {e}")
        return None

    for school in data.get("schools", []):
        if school.get("school_id") == school_id:
            return school
        if school_code and school.get("school_code") == school_code:
            return school
    return None


async def ensure_school_exists(school_id: int, school_code: str = None) -> bool:
    """Ensure the school exists locally; fall back to local JSON then remote API."""
    async for db in get_db():
        from sqlalchemy import select

        from models import TsnSchool_Model
        from services.tsnSchool.tsnSchoolDao import addOrUpdateSchool

        stmt = select(TsnSchool_Model).where(TsnSchool_Model.school_id == school_id)
        result = await db.execute(stmt)
        school = result.scalar_one_or_none()
        if school:
            logger.info(f"学校已存在: {school.school_name} (ID: {school_id})")
            return True

        if not school_code:
            logger.error(f"学校 {school_id} 不存在且未提供 school_code，无法自动创建")
            return False

        local_school = get_school_info_from_local_file(school_id, school_code)
        if local_school:
            logger.info(f"学校 {school_id} 不存在，改用本地 school_data.json 补充学校信息...")
            await addOrUpdateSchool(
                schoolId=local_school["school_id"],
                schoolName=local_school["school_name"],
                schoolUrl=local_school.get("school_url", ""),
                lanUrl=None,
                openId=local_school.get("openId", ""),
                isOpenKeep=local_school.get("isOpenKeep") == "1",
                isOpenLive=local_school.get("isOpenLive") == "1",
                isOpenEncry=local_school.get("isOpenEncry") == "1",
                sys_type=int(local_school.get("sysType", 2)),
                school_code=local_school["school_code"],
                session=db,
            )
            logger.success(f"学校 {local_school['school_name']} 已从本地数据写入")
            return True

        logger.info(f"学校 {school_id} 不存在，尝试从远程获取 (school_code={school_code})...")
        school_info = await get_school_info_from_api(school_code)
        if not school_info:
            logger.error(f"获取学校信息失败: school_code={school_code}")
            return False

        await addOrUpdateSchool(
            schoolId=school_info["schoolId"],
            schoolName=school_info["schoolName"],
            schoolUrl=school_info.get("url", ""),
            lanUrl=f"https://{school_info['url']}" if school_info.get("url") else None,
            openId=school_info.get("openId"),
            isOpenKeep=school_info.get("isOpenKeep") == "1",
            isOpenLive=school_info.get("isOpenLive") == "1",
            isOpenEncry=school_info.get("isOpenEncry") == "1",
            sys_type=int(school_info.get("sysType", 2)),
            school_code=school_code,
            session=db,
        )
        logger.success(f"学校 {school_info['schoolName']} 插入成功")
        return True


async def auto_authorize_and_get_account(
    school_id: int, username: str, password: str, school_code: str = None
) -> Optional[int]:
    if not await ensure_school_exists(school_id, school_code):
        return None

    async for db in get_db():
        try:
            raw_uid = await tsnPasswordAuthServer(school_id, username, password, db)
            logger.info(f"授权成功，原始返回 uid = {raw_uid} (type: {type(raw_uid)})")
        except Exception as e:
            logger.error(f"授权失败: {e}")
            return None

        if isinstance(raw_uid, str) and ":" in raw_uid:
            real_uid_str = raw_uid.split(":")[-1]
        elif isinstance(raw_uid, (str, int)):
            real_uid_str = str(raw_uid)
        else:
            logger.error(f"未知的 uid 类型: {raw_uid}")
            return None

        from sqlalchemy import select

        stmt = select(TsnAccount_Model).where(
            TsnAccount_Model.user_id == real_uid_str,
            TsnAccount_Model.school_id == school_id,
        )
        result = await db.execute(stmt)
        account = result.scalar_one_or_none()

        if account:
            logger.info(f"找到账号记录: id={account.id}, user_id={account.user_id}")
            return account.id

        logger.error(f"授权成功但未找到账号记录: user_id={real_uid_str}, school_id={school_id}")
        return None


async def auto_run():
    username = os.getenv("ACCOUNT_USERNAME")
    password = os.getenv("ACCOUNT_PASSWORD")
    school_id_str = os.getenv("SCHOOL_ID")
    school_code = os.getenv("SCHOOL_CODE")
    distance_str = os.getenv("RUN_DISTANCE", "2.5")
    run_type_str = os.getenv("RUN_TYPE", "sunrun")

    missing = []
    if not username:
        missing.append("ACCOUNT_USERNAME")
    if not password:
        missing.append("ACCOUNT_PASSWORD")
    if not school_id_str:
        missing.append("SCHOOL_ID")
    if not school_code:
        missing.append("SCHOOL_CODE")
    if missing:
        logger.error("缺少以下 GitHub Secrets:")
        for item in missing:
            logger.error(f"  - {item}")
        sys.exit(1)

    try:
        school_id = int(school_id_str)
    except ValueError:
        logger.error(f"SCHOOL_ID 必须是数字，当前值: {school_id_str}")
        sys.exit(1)

    try:
        distance = float(distance_str)
        if distance <= 0 or distance > MAX_DISTANCE:
            logger.error(f"距离 {distance}km 超出允许范围 (0, {MAX_DISTANCE}]")
            sys.exit(1)
    except ValueError:
        logger.error(f"距离格式错误: {distance_str}")
        sys.exit(1)

    run_type_map = {
        "morningrun": TsnRunType.morningRun,
        "sunrun": TsnRunType.sumRun,
        "freedom": TsnRunType.freedom,
    }
    run_type = run_type_map.get(run_type_str, TsnRunType.sumRun)
    run_type_name = {
        "morningrun": "晨跑",
        "sunrun": "阳光跑",
        "freedom": "自由跑",
    }.get(run_type_str, "阳光跑")

    logger.info("=" * 60)
    logger.info("开始自动刷步数任务")
    logger.info(f"账号: {username}")
    logger.info(f"学校ID: {school_id}")
    logger.info(f"学校代码: {school_code}")
    logger.info(f"跑步类型: {run_type_name}")
    logger.info(f"跑步距离: {distance} km")
    logger.info("=" * 60)

    await init_db()

    account_id = await auto_authorize_and_get_account(
        school_id, username, password, school_code=school_code
    )
    if not account_id:
        logger.error("无法获得有效的账号记录，终止运行")
        sys.exit(1)

    logger.info(f"开始执行跑步任务，account_id={account_id}")
    run_server = TsnRunServer(
        accountId=account_id,
        runKiloMeter=distance,
        logRunType=run_type,
    )
    await run_server.startRunHandle()

    logger.success(f"刷步数完成，共 {distance} km")
    logger.info("=" * 60)


async def main():
    logger.remove()
    logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )
    logger.add("run.log", rotation="10 MB", level="DEBUG")
    await auto_run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n程序被用户中断")
    except Exception as e:
        logger.exception(e)
        sys.exit(1)
