"""事实模型与带时间截点的资料仓库。

资料包（history.json / updates.json 及后续包）只追加、不改写：
* 每条事实保留来源包、获知时间（known_at）与原始负载；
* ``revises`` 表示对旧事实身份的更正链，不删除历史；
* 同一行商品的多条 returns 是不同获知时点的“累计快照”，绝不相加；
* 所有 as-of 查询只返回 ``known_at <= 截点`` 的资料，occurred/effective 与
  known 两类时间分别服务于“发生时点”和“可获知时点”，不可互换。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from dateutil.parser import isoparse

MATERIAL_TABLES = (
    "baskets", "lines", "discounts", "cohort", "observations",
    "returns", "claims", "confirmed_funding", "budgets", "offers",
    "accepted_plans",
)


def parse_ts(value: str):
    """解析带时区的 ISO 8601 字符串；调用方负责保证时区感知对象之间比较。"""
    return isoparse(value)


@dataclass(frozen=True)
class Fact:
    table: str
    fid: str
    payload: dict[str, Any]
    source: str
    known_at: Any  # datetime 或 None（无该字段的表）
    revises: str | None = None


@dataclass
class FactStore:
    """合并多个资料包后的不可变事实仓库（按导入顺序）。"""

    facts: dict[tuple[str, str], Fact] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    # ---- 构建 ----
    @classmethod
    def load(cls, paths: Iterable[str | Path]) -> "FactStore":
        store = cls()
        for path in paths:
            store.ingest_package(Path(path))
        return store

    def ingest_package(self, path: Path) -> None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("synthetic") is not True or not isinstance(doc.get("tables"), dict):
            raise ValueError(f"资料包格式无效: {path.name}")
        self.sources.append(path.name)
        for table, rows in doc["tables"].items():
            if table not in MATERIAL_TABLES:
                raise ValueError(f"未知数据表 {table}（来源 {path.name}）")
            for row in rows:
                key = (table, row["id"])
                if key in self.facts:
                    raise ValueError(f"事实身份重复: {table}/{row['id']}")
                known = row.get("known_at") or row.get("confirmed_at") \
                    or row.get("accepted_at") or row.get("eligible_from")
                self.facts[key] = Fact(
                    table=table,
                    fid=row["id"],
                    payload=dict(row),
                    source=path.name,
                    known_at=parse_ts(known) if known else None,
                    revises=row.get("revises"),
                )
        self._validate_references()

    def _validate_references(self) -> None:
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "input-schema.json").read_text(encoding="utf-8")
        )
        ids = {(t, f.fid) for (t, _), f in self.facts.items()}
        for table, fkey, target, key in schema["references"]:
            target_ids = {f.payload[key] for (t, _), f in self.facts.items() if t == target}
            for (t, _), fact in self.facts.items():
                if t == table and fact.payload.get(fkey) not in target_ids:
                    raise ValueError(
                        f"外键缺失: {table}.{fkey}={fact.payload.get(fkey)} "
                        f"不在 {target}.{key} 中"
                    )

    # ---- 全量视图 ----
    def rows(self, table: str) -> list[dict[str, Any]]:
        return [f.payload for (t, _), f in self.facts.items() if t == table]

    def fact(self, table: str, fid: str) -> Fact:
        return self.facts[(table, fid)]

    def has(self, table: str, fid: str) -> bool:
        return (table, fid) in self.facts

    # ---- as-of 视图 ----
    def as_of(self, table: str, cutoff) -> list[dict[str, Any]]:
        """截点已可获知的该表全部事实（原始记录，未做更正合并）。"""
        out = []
        for (t, _), f in self.facts.items():
            if t != table:
                continue
            if f.known_at is None or f.known_at <= cutoff:
                out.append(f.payload)
        return out

    def effective_returns(self, cutoff) -> dict[str, dict[str, Any]]:
        """行商品 -> 截点有效的最新累计退货快照（同 line_id 取 known_at 最大者）。"""
        latest: dict[str, dict[str, Any]] = {}
        for (t, _), f in self.facts.items():
            if t != "returns" or (f.known_at is not None and f.known_at > cutoff):
                continue
            row = f.payload
            old = latest.get(row["line_id"])
            if old is None or parse_ts(row["known_at"]) > parse_ts(old["known_at"]):
                latest[row["line_id"]] = row
        return latest

    def effective_claims(self, cutoff) -> dict[tuple[str, str, str], dict[str, Any]]:
        """更正链折叠后的有效异议，键为 (basket_id, target, target_id)。

        链上每条记录按 known_at 排序；截点内取最新一条。pending 记录保留在
        原始事实中可回查，但本视图会标注其状态，由领域层决定是否生效。
        """
        chains: dict[str, list[dict[str, Any]]] = {}
        for (t, _), f in self.facts.items():
            if t != "claims" or (f.known_at is not None and f.known_at > cutoff):
                continue
            chains.setdefault(f.fid, []).append(f.payload)
            if f.revises:
                chains.setdefault(f.revises, []).append(f.payload)
        effective: dict[tuple[str, str, str], dict[str, Any]] = {}
        for records in chains.values():
            records = sorted(records, key=lambda r: parse_ts(r["known_at"]))
            latest = records[-1]
            key = (latest["basket_id"], latest["target"], latest["target_id"])
            old = effective.get(key)
            if old is None or parse_ts(latest["known_at"]) > parse_ts(old["known_at"]):
                effective[key] = latest
        return effective

    def effective_confirmed_funding(self, cutoff) -> list[dict[str, Any]]:
        """截点前已确认的出资（confirmed_at 为可获知时间）。"""
        return [
            f.payload
            for (t, _), f in self.facts.items()
            if t == "confirmed_funding" and (f.known_at is None or f.known_at <= cutoff)
        ]
