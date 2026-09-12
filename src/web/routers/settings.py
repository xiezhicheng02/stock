# -*- coding: utf-8 -*-
"""设置接口：配置分组（节） + 配置项的增删改查。

分组采用 {title, keys} 结构：一个"节"有标题 + 一组配置键（有序）。
这样可以把多个数据库 group 合并成一个节（如"定时任务"= runtime+scheduler），
或把一个 group 拆成多个节（signal → 估值区间与信号 + 推荐操作与配色）。
节列表存于 SETTING_GROUPS；未配置/为空时回退到 defaults.SETTING_GROUPS。
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.config import config
from src.config.defaults import DEFAULT_SETTINGS
from src.config.defaults import SETTING_GROUPS as _DEFAULT_GROUPS
from src.web import deps
from src.web import scheduler as sched

log = logging.getLogger("web.settings")
router = APIRouter(prefix="/api", tags=["settings"])


class SettingBody(BaseModel):
    value: object


class SettingCreate(BaseModel):
    key: str
    value: object
    group: str = "未分组"
    remark: str = ""
    section: str = ""          # 要加入的节标题（可选）


class SectionBody(BaseModel):
    title: str


# 配置节的两份定义会漂移，因此这里**直接沿用** defaults.SETTING_GROUPS：
# 新增配置项只需改 src/config/defaults.py 一处，设置页与新建库自然一致。
_DEFAULT_SECTIONS = [dict(s) for s in _DEFAULT_GROUPS]

# 定时任务的配置节：按当前系统的**三个任务**划分（拉取数据 / 计算指标 / 通知）
SCHED_SECTIONS = [dict(s) for s in _DEFAULT_GROUPS if "定时任务" in s.get("title", "")]

# 老库里的旧节名 → 自动拆成上面这几节
_OLD_SCHED_TITLES = ("⏰ 定时任务",)

# 老库里的旧节名 → 现在的节名（重命名时统一在这里登记，老库自动改名）
_RENAMED_SECTIONS = {
    "📥 拉取数据": "📥 数据源（baostock）",
}

# 键 → 它在 defaults.SETTING_GROUPS 里所属的节名（新增键优先按它归位）
_DEFAULT_SECTION_OF = {
    k: s.get("title")
    for s in _DEFAULT_SECTIONS for k in s.get("keys", [])
}

# 老库补齐用（defaults 里的节名在老库不存在时，退回关键字匹配）
KEY_SECTION_HINT = {
    "DIVERGENCE_THRESHOLD": ("估值区间", "信号"),
    "DIVIDEND_YEARS_BACK": ("数据源", "baostock", "抓取"),
    "REBUILD_WORKERS": ("数据源", "baostock", "抓取"),
    "SYNC_BEFORE_SCORE": ("数据源", "baostock", "抓取", "设置"),
}

# 改了这些配置要让调度器立即生效（不用重启服务）
TRIGGER_SCHEDULE_KEYS = {"SYNC_RUN_TIME", "INDICATORS_INTERVAL_MINUTES",
                         "NOTIFY_BUILD_TIME", "NOTIFY_SEND_TIME", "NOTIFY_RESEND_TIMES"}
STRUCT_SCHEDULE_KEYS = {"SCHEDULER_TIMEZONE", "SCHEDULER_MISFIRE_GRACE",
                        "SCHEDULER_COALESCE", "SCHEDULER_MAX_INSTANCES"}
SCHEDULE_KEYS = (TRIGGER_SCHEDULE_KEYS | STRUCT_SCHEDULE_KEYS
                | {"SCHEDULER_ENABLED", "SKIP_NON_TRADING_DAY"})


def _decoded(value, val_type: str):
    """把库里存的字符串按 val_type 还原成 Python 值。"""
    if value is None:
        return None
    if val_type == "json":
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    if val_type == "int":
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return None
    if val_type == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    if val_type == "bool":
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    return value


def _coerce(raw, val_type: str):
    """把前端提交的值按 val_type 转成 Python 值。"""
    if val_type == "json":
        if isinstance(raw, (dict, list)):
            return raw
        if raw is None or raw == "":
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"不是合法 JSON：{raw!r}") from e
    if val_type == "int":
        try:
            return int(float(raw))
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"不是整数：{raw!r}") from e
    if val_type == "float":
        try:
            return float(raw)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"不是数字：{raw!r}") from e
    if val_type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if val_type == "none":
        return None if raw in (None, "") else raw
    return None if raw is None else str(raw)


def _ensure_keys(sections: list) -> bool:
    """把程序新增、老库分组里没有的键补进对应的节。返回是否有改动。

    归位顺序：① 按 defaults.SETTING_GROUPS 里该键所属的**节名**（最准，不会把
    "抓取参数"错放进"拉取数据定时任务"节）；② 该节名在老库不存在时，退回
    KEY_SECTION_HINT 的关键字匹配。
    """
    placed = {k for s in sections for k in s.get("keys", [])}
    titles = {s.get("title") for s in sections}
    changed = False
    for key, *_rest in DEFAULT_SETTINGS:
        if key in placed:
            continue
        target = None
        want = _DEFAULT_SECTION_OF.get(key)
        if want and want in titles:
            target = next((s for s in sections if s.get("title") == want), None)
        if target is None:
            for h in KEY_SECTION_HINT.get(key, ()):
                target = next((s for s in sections if h in s.get("title", "")), None)
                if target is not None:
                    break
        if target is not None:
            target.setdefault("keys", []).append(key)
            changed = True
    return changed


def _prune_keys(sections: list, known: set) -> bool:
    """剔除已从系统里删掉的配置键（老库残留），返回是否有改动。

    例如 BAOSTOCK_ADJUSTFLAG / SCHEDULER_HEARTBEAT_MINUTES 已废弃，
    config.ensure_settings() 会把它们从 setting 表删掉，但 SETTING_GROUPS
    里仍留着引用，设置页就会渲染出空壳配置项。
    """
    changed = False
    for s in sections:
        keys = [k for k in s.get("keys", []) if k in known]
        if keys != s.get("keys"):
            s["keys"] = keys
            changed = True
    return changed


def _sections() -> list:
    """当前配置节列表（{title, keys}）。

    SETTING_GROUPS 空/旧格式时回退默认；老库里的「⏰ 定时任务」一节会自动
    拆成按当前三个任务划分的 4 节，程序新增的键补进对应节、已删除的键剔除
    （各只做一次）。
    """
    g = config.get_json("SETTING_GROUPS", []) or []
    if not (g and all(isinstance(x, dict) and "keys" in x for x in g)):
        return [dict(s) for s in _DEFAULT_SECTIONS]
    changed = False
    out = []
    for s in g:
        title = s.get("title")
        if title in _OLD_SCHED_TITLES:
            out.extend(dict(x) for x in SCHED_SECTIONS)
            changed = True
        elif title in _RENAMED_SECTIONS:
            out.append(dict(s, title=_RENAMED_SECTIONS[title]))
            changed = True
        else:
            out.append(s)
    # 节改名后万一和已有节重名，丢弃后面的那份（配置键由 _ensure_keys 补回）
    seen, uniq = set(), []
    for s in out:
        if s.get("title") in seen:
            changed = True
            continue
        seen.add(s.get("title"))
        uniq.append(s)
    out = uniq
    # 这几节是程序管理的，键列表以代码为准
    for s in out:
        for tpl in SCHED_SECTIONS:
            if s.get("title") == tpl["title"] and s.get("keys") != tpl["keys"]:
                s["keys"] = list(tpl["keys"])
                changed = True
    known = {it["key"] for it in config.all_settings()}
    changed = _prune_keys(out, known) or changed
    changed = _ensure_keys(out) or changed
    if changed:
        try:
            _save_sections(out)
            log.info("配置节已自动升级（定时任务拆分为 %d 节 + 新增键归位 + 废弃键清理）",
                     len(SCHED_SECTIONS))
        except Exception as e:                      # noqa: BLE001
            log.warning("配置节升级写回失败（本次仍按新结构展示）：%s", e)
    return out


def _save_sections(sections: list):
    config.set("SETTING_GROUPS", sections, group="web",
               remark="配置分组及展示顺序（程序自动维护）")


def _remove_key_from_sections(key: str, sections: list) -> list:
    for s in sections:
        if key in s.get("keys", []):
            s["keys"] = [k for k in s["keys"] if k != key]
    return sections


@router.get("/settings/editable", summary="配置节 + 配置项（可编辑视图）")
def settings_editable(_: None = Depends(deps.require_auth)):
    """返回配置节列表（{title, keys}）与全部配置项（扁平，带 group/类型/打码值）。"""
    items = {}
    for it in config.all_settings():
        value = _decoded(it["value"], it["val_type"])
        items[it["key"]] = {
            "key": it["key"],
            "value": value,                       # 直接展示库中内容，不脱敏
            "val_type": it["val_type"],
            "remark": it["remark"],
            "secret": deps.is_secret(it["key"]),
            "group": it["group"] or "未分组",
        }
    return {"sections": _sections(), "items": items}


@router.post("/settings", summary="新增配置项")
def settings_create(body: SettingCreate, _: None = Depends(deps.require_auth)):
    key = body.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="配置键不能为空")
    if config.exists(key):
        raise HTTPException(status_code=409, detail=f"配置项已存在：{key}")
    try:
        config.set(key, body.value, group=body.group or "未分组", remark=body.remark)
    except Exception as e:                      # noqa: BLE001
        log.exception("新增配置失败 %s", key)
        raise HTTPException(status_code=400, detail=f"新增配置失败：{deps.safe_msg(e)}")

    # 若指定了节，把 key 加进该节的 keys
    if body.section:
        sections = _sections()
        hit = next((s for s in sections if s.get("title") == body.section), None)
        if hit is None:
            hit = {"title": body.section, "keys": []}
            sections.append(hit)
        if key not in hit["keys"]:
            hit["keys"].append(key)
        _save_sections(sections)
    return {"ok": True, "key": key, "section": body.section}


@router.delete("/settings/{key}", summary="删除配置项")
def settings_delete(key: str, _: None = Depends(deps.require_auth)):
    if not config.exists(key):
        raise HTTPException(status_code=404, detail=f"配置项不存在：{key}")
    config.delete(key)
    _save_sections(_remove_key_from_sections(key, _sections()))
    return {"ok": True, "key": key}


@router.post("/settings/sections", summary="新增配置分组（节）")
def section_add(body: SectionBody, _: None = Depends(deps.require_auth)):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="分组标题不能为空")
    sections = _sections()
    if any(s.get("title") == title for s in sections):
        raise HTTPException(status_code=409, detail=f"分组已存在：{title}")
    sections.append({"title": title, "keys": []})
    _save_sections(sections)
    return {"ok": True, "title": title}


@router.delete("/settings/sections/{title}", summary="删除配置分组（节）")
def section_delete(title: str, _: None = Depends(deps.require_auth)):
    sections = [s for s in _sections() if s.get("title") != title]
    _save_sections(sections)
    return {"ok": True, "title": title}


@router.put("/settings/{key}", summary="写回单个配置项")
def settings_put(key: str, body: SettingBody, _: None = Depends(deps.require_auth)):
    cur = config.all_settings()
    hit = next((it for it in cur if it["key"] == key), None)
    if hit is None:
        raise HTTPException(status_code=404, detail=f"配置项不存在：{key}")

    try:
        value = _coerce(body.value, hit["val_type"])
    except HTTPException:
        raise
    try:
        config.set(key, value)
    except Exception as e:                      # noqa: BLE001
        log.exception("写配置失败 %s", key)
        raise HTTPException(status_code=400, detail=f"写配置失败：{deps.safe_msg(e)}")

    # 定时任务相关配置 → 立刻让调度器按新配置生效（不用重启服务）
    sched_info = None
    if key in SCHEDULE_KEYS:
        try:
            sched_info = sched.apply_config(structural=(key in STRUCT_SCHEDULE_KEYS))
        except Exception as e:                  # noqa: BLE001
            log.exception("让调度配置生效失败 %s", key)
            sched_info = {"ok": False,
                          "msg": f"配置已保存，但定时任务未能更新：{deps.safe_msg(e)}"}
    return {"ok": True, "key": key, "value": value, "changed": True,
            "scheduler": sched_info}
