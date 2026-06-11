#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Automated running workflow with robust school bootstrap options."""

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


def parse_bool_env(value: Optional[str], default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def get_school_info_from_local_file(school_id: int, school_code: Optional[str] = None):
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


async def get_school_info_from_api(school_code: str):
    import httpx

    url = f"https://h.tsnkj.com/upms/sysSchool/getSchoolInfo?schoolCode={school_code}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"User-Agent": "okhttp/4.9.0"})
            if resp.status_code != 200:
                logger.error(f"学校信息接口返回非 200 状态码: {resp.status_code}")
                return None

            data = resp.json()
            if data.get("code") not in (200, 0):
                logger.error(f"学校信息接口返回错误: {data.get('msg', '未知错误')}")
                return None

            school_data = data.get("data")
            if not school_data:
                logger.error("学校信息接口返回的 data 字段为空")
                return None

            return school_data
    except httpx.TimeoutException:
        logger.error("请求学校信息超时")
        return None
    except Exception as e:
        logger.error(f"请求学校信息异常: {e}")
        return None


def read_school_seed_from_env(school_id: int, school_code: str) -> dict:
    sys_type_raw = os.getenv("SYS_TYPE")
    sys_type = None
    if sys_type_raw:
        try:
            sys_type = int(sys_type_raw)
        except ValueError:
            logger.warning(f"忽略非法 SYS_TYPE: {sys_type_raw}")

    return {
        "school_id": school_id,
        "school_code": school_code,
        "school_name": os.getenv("SCHOOL_NAME"),
        "school_url": normalize_url(os.getenv("SCHOOL_URL")),
        "lan_url": normalize_url(os.getenv("LAN_URL")),
        "open_id": os.getenv("OPEN_ID"),
        "sys_type": sys_type,
        "is_open_keep": parse_bool_env(os.getenv("IS_OPEN_KEEP"), False),
        "is_open_live": parse_bool_env(os.getenv("IS_OPEN_LIVE"), False),
        "is_open_encry": parse_bool_env(os.getenv("IS_OPEN_ENCRY"), False),
    }


def merge_school_sources(
    school_id: int,
    school_code: str,
    env_seed: dict,
    local_school: Optional[dict] = None,
    api_school: Optional[dict] = None,
) -> dict:
    merged = {
        "school_id": school_id,
        "school_code": school_code,
        "school_name": None,
        "school_url": None,
        "lan_url": None,
        "open_id": None,
        "sys_type": None,
        "is_open_keep": False,
        "is_open_live": False,
        "is_open_encry": False,
    }

    if local_school:
        merged.update(
            {
                "school_id": local_school.get("school_id", merged["school_id"]),
                "school_code": local_school.get("school_code", merged["school_code"]),
                "school_name": local_school.get("school_name"),
            }
        )

    if api_school:
        merged.update(
            {
                "school_id": api_school.get("schoolId", merged["school_id"]),
                "school_code": api_school.get("schoolCode", merged["school_code"]),
                "school_name": api_school.get("schoolName", merged["school_name"]),
                "school_url": normalize_url(api_school.get("schoolUrl") or api_school.get("school_url")),
                "lan_url": normalize_url(api_school.get("url")),
                "open_id": api_school.get("openId"),
                "sys_type": int(api_school.get("sysType", 2)),
                "is_open_keep": api_school.get("isOpenKeep") == "1",
                "is_open_live": api_school.get("isOpenLive") == "1",
                "is_open_encry": api_school.get("isOpenEncry") == "1",
            }
        )

    for key, value in env_seed.items():
        if value is not None:
            merged[key] = value

    return merged


def is_school_seed_usable(seed: dict) -> tuple[bool, list[str]]:
    missing = []

    if not seed.get("school_code"):
        missing.append("school_code")
    if not seed.get("school_name"):
        missing.append("school_name")

    sys_type = seed.get("sys_type")
    if sys_type not in (1, 2):
        missing.append("sys_type")
        return False, missing

    if not seed.get("open_id"):
        missing.append("open_id")

    if sys_type == 1 and not seed.get("school_url"):
        missing.append("school_url")

    if sys_type == 2 and not seed.get("lan_url"):
        missing.append("lan_url")

    return len(missing) == 0, missing


async def save_school_seed(seed: dict, db) -> None:
    from services.tsnSchool.tsnSchoolDao import addOrUpdateSchool

    await addOrUpdateSchool(
        schoolId=seed["school_id"],
        schoolName=seed["school_name"],
        schoolUrl=seed.get("school_url") or "",
        lanUrl=seed.get("lan_url"),
        openId=seed.get("open_id") or "",
        isOpenKeep=seed.get("is_open_keep", False),
        isOpenLive=seed.get("is_open_live", False),
        isOpenEncry=seed.get("is_open_encry", False),
        sys_type=seed["sys_type"],
        school_code=seed["school_code"],
        session=db,
    )


async def ensure_school_exists(school_id: int, school_code: str) -> bool:
    async for db in get_db():
        from sqlalchemy import select

        from models import TsnSchool_Model

        stmt = select(TsnSchool_Model).where(TsnSchool_Model.school_id == school_id)
        result = await db.execute(stmt)
        school = result.scalar_one_or_none()
        if school:
            logger.info(f"学校已存在: {school.school_name} (ID: {school_id})")
            return True

        env_seed = read_school_seed_from_env(school_id, school_code)
        local_school = get_school_info_from_local_file(school_id, school_code)
        api_school = await get_school_info_from_api(school_code)

        merged_seed = merge_school_sources(
            school_id=school_id,
            school_code=school_code,
            env_seed=env_seed,
            local_school=local_school,
            api_school=api_school,
        )
        usable, missing = is_school_seed_usable(merged_seed)

        if not usable:
            logger.error(
                "无法构造完整学校配置，缺少字段: "
                + ", ".join(missing)
            )
            logger.error(
                "请在 GitHub Secrets 中补充这些字段，或等待学校信息接口恢复后再重试"
            )
            return False

        source_parts = []
        if api_school:
            source_parts.append("远程接口")
        if local_school:
            source_parts.append("本地 school_data.json")
        if any(v is not None for v in env_seed.values() if v != school_id and v != school_code):
            source_parts.append("GitHub Secrets")
        source_text = " + ".join(source_parts) if source_parts else "默认配置"
        logger.info(f"学校 {school_id} 不存在，使用 {source_text} 补充学校信息...")

        await save_school_seed(merged_seed, db)
        logger.success(f"学校 {merged_seed['school_name']} 已写入本地数据库")
        return True


async def auto_authorize_and_get_account(
    school_id: int, username: str, password: str, school_code: str
) -> Optional[int]:
    if not await ensure_school_exists(school_id, school_code):
        return None

    async for db in get_db():
        try:
            raw_uid = await tsnPasswordAuthServer(school_id, username, password, db)
            logger.info(f"授权成功，原始返回 uid = {raw_uid} (type: {type(raw_uid)})")
        except Exception as e:
            logger.error(f"授权失败: {type(e).__name__}: {e!r}")
            logger.exception("授权异常堆栈")
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
