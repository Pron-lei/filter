"""内容寻址哈希。

两类用途：
  1. 媒体哈希 —— 原始/派生各自存哈希，用于溯源与冻结（plan §6.1.1）
  2. stage 指纹 —— 代码版本 + 配置 + 上游产物哈希，决定缓存是否失效
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def canon_json(obj: Any) -> str:
    """规范化 JSON：排序键、紧凑分隔符。保证哈希对字典顺序不敏感。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def obj_sha256(obj: Any) -> str:
    return hashlib.sha256(canon_json(obj).encode()).hexdigest()


def stage_fingerprint(stage_name: str, stage_version: str,
                      config: dict, upstream_hashes: list[str]) -> str:
    """stage 的内容寻址指纹。任一输入变化 -> 指纹变化 -> 自动重跑。"""
    return obj_sha256({
        "stage": stage_name,
        "version": stage_version,
        "config": config,
        "upstream": sorted(upstream_hashes),
    })[:16]


def media_fingerprint_pair(raw_path: str | Path, derived_path: str | Path | None) -> dict:
    """原始与派生分别存哈希 —— plan 明确要求二者不可混为一谈。"""
    out = {"raw_sha256": file_sha256(raw_path)}
    if derived_path is not None and Path(derived_path).exists():
        out["derived_sha256"] = file_sha256(derived_path)
    return out


def artifact_hash_or_empty(stage_dir) -> str:
    """上游尚未产出时返回空串，让首个 stage 也能计算指纹。"""
    from pathlib import Path
    from .io import artifact_hash
    p = Path(stage_dir)
    if not p.exists():
        return ""
    return artifact_hash(p)
