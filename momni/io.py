"""JSONL / manifest / 原子写。

约定：
  * 每个 stage 目录结构固定：
      part-XXXXX.jsonl   分片产物
      _manifest.json     指纹 + 产物哈希 + 上游引用
      _stats.json        计数与留存率
      _failures.jsonl    失败样本（带 traceback），**不静默丢弃**
      _rejects.jsonl     规则拒绝样本（带 reason code）
  * 所有写入先写 .tmp 再 os.replace，避免半截文件污染缓存
"""
from __future__ import annotations
import json
import threading, os, time, random
from pathlib import Path
from typing import Any, Iterable, Iterator

from .hashing import obj_sha256


# --- 共享盘 EMFILE 退避 ---------------------------------------------------
# CPFS/virtiofs 共享盘会在**其他租户**把挂载点的句柄池压满时抛 EMFILE。
# 实测出错当时：系统 file-nr 2688/9848858、ulimit -n 1048576、本进程仅十几个 fd ——
# 不是我们句柄用尽，所以调小 ulimit 或减少 fd 都没用，只能等对方松手。
# bash 层「整个分片重跑」太贵（S01b 一个分片几十分钟）；退避重试放在写入点最便宜。
_EMFILE = {23, 24}  # ENFILE / EMFILE


def _retry_io(fn, tries: int = 16, base: float = 1.0, cap: float = 45.0):
    """幂等 IO 操作的退避重试。**只用于可安全重放的操作**。

    退避要**封顶 + 总预算长**，不能纯指数。实测饱和窗口可达数分钟：
    一次 `tries=8` 的纯指数（1,2,4,...,128s）总预算才 ~4 分钟就放弃了，
    而当时挂载点的可开文件数已掉到 2~3 个（整机只有 29 个文件开在上面，
    连 python 启动读 /cpfs01 上的 .pth 都失败 —— 是 virtiofsd 自身饱和，
    与本进程 fd 无关）。封顶 45s、16 次，总预算约 10 分钟。"""
    for i in range(tries):
        try:
            return fn()
        except OSError as e:
            if e.errno not in _EMFILE or i == tries - 1:
                raise
            time.sleep(min(base * (2 ** i), cap) * (0.5 + random.random()))


def _ropen(*a, **kw):
    """只重试「拿到句柄」这一步；写到一半失败不会重放（避免追加写重复行）。"""
    return _retry_io(lambda: open(*a, **kw))


def read_jsonl(path: str | Path) -> Iterator[dict]:
    p = Path(path)
    if not p.exists():
        return
    with _ropen(p, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{p}:{ln} bad json: {e}") from e


def read_stage(stage_dir: str | Path) -> Iterator[dict]:
    """读一个 stage 的全部 part-*.jsonl（按文件名排序，保证确定性）。"""
    for part in sorted(Path(stage_dir).glob("part-*.jsonl")):
        yield from read_jsonl(part)


def write_jsonl(path: str | Path, rows: Iterable[dict], append: bool = False) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if append:
        n = 0
        with _ropen(p, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                n += 1
        return n
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.{threading.get_ident():x}.tmp")
    rows = list(rows)          # 重放需要可重复迭代

    def _do():
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
        return len(rows)

    return _retry_io(_do)


def write_json(path: str | Path, obj: Any) -> None:
    """原子写。临时文件名必须**进程唯一**。

    原来用 `p.with_suffix(".tmp")` —— 固定名字，跨进程共享。
    12 个分片同时写 `_manifest.json` 时，各自写同一个 `_manifest.tmp`，
    先 rename 的那个把文件搬走，其余的在 os.replace 上拿
    `FileNotFoundError: '_manifest.tmp' -> '_manifest.json'`。
    实测 S02/S03 各抛了 5~6 次。更糟的是：
    `for ... & done; wait` 的 `wait` **不带参数时恒返回 0**，
    所以 `set -e` 根本拦不住，分片失败管线照样往下走。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.{threading.get_ident():x}.tmp")
    def _do():
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, p)

    try:
        _retry_io(_do)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with _ropen(p, "r", encoding="utf-8") as f:
        return json.load(f)


def append_only(path: str | Path, row: dict) -> None:
    """审核/裁决日志专用：只追加，带时间戳与行哈希，事后不可悄悄改写。"""
    row = dict(row)
    row.setdefault("_ts", time.time())
    row["_row_sha"] = obj_sha256({k: v for k, v in row.items() if k != "_row_sha"})[:16]
    write_jsonl(path, [row], append=True)


def stage_manifest(stage_dir: str | Path) -> dict:
    return read_json(Path(stage_dir) / "_manifest.json", default={}) or {}


def artifact_hash(stage_dir: str | Path) -> str:
    """一个 stage 全部产物的聚合哈希，作为下游指纹的输入。"""
    parts = sorted(Path(stage_dir).glob("part-*.jsonl"))
    from .hashing import file_sha256
    return obj_sha256([f"{p.name}:{file_sha256(p)}" for p in parts])[:16]
