"""⑧' 需給の R2 時系列 (supply/{code}.json) と D1 断面への書き込み。

R2 のオブジェクトは **全置換 PUT** なので、既存の系列を読んでから追記し、
後退禁止ガード (guards.check_no_regression) を必ず通す。日証金は最新
スナップショットしか公開しないため、取り逃した日は永久に埋まらない。
ここで系列を縮めると復元できない。

ライセンスは personal-only 固定。公開 Worker がバインドしている vwap-data とは
別バケット (jp-stock-supply) に置き、R2 トークンのスコープでも分離する。
"""

from __future__ import annotations

import logging
from datetime import date

from .r2 import R2Store

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# supply/{code}.json の契約キー。壊すと消費側が読めなくなる。
CONTRACT: dict[str, tuple[str, ...]] = {
    "$": ("code", "schema", "updated", "series"),
}


def merge_series(
    existing: list[dict] | None, points: list[dict], *, key_fields: tuple[str, ...] = ("d", "ex")
) -> list[dict]:
    """既存系列へ points を追記する。同一キーの点は新しい値で置き換える。

    **既存の点は決して落とさない。** 同じ (日付, 取引所) の再取得は上書きだが、
    別の日付・別の取引所の既存点はそのまま残す。返す配列は日付昇順。
    """
    merged: dict[tuple, dict] = {}
    for item in existing or []:
        merged[tuple(item.get(f) for f in key_fields)] = item
    for item in points:
        merged[tuple(item.get(f) for f in key_fields)] = item
    return sorted(merged.values(), key=lambda x: (str(x.get("d") or ""), str(x.get("ex") or "")))


def build_payload(
    code: str,
    *,
    existing: dict | None,
    by_type: dict[str, list[dict]],
    updated: date,
    writer: str,
) -> dict:
    """supply/{code}.json の全置換 payload を組み立てる。"""
    old_series = (existing or {}).get("series") or {}
    series: dict[str, list[dict]] = {k: list(v) for k, v in old_series.items()}
    for data_type, points in by_type.items():
        series[data_type] = merge_series(series.get(data_type), points)
    return {
        "code": code,
        "schema": SCHEMA_VERSION,
        "writer": writer,
        "updated": updated.isoformat(),
        "license": "personal-only",
        "series": series,
    }


def upsert_supply_series(
    store: R2Store, code: str, by_type: dict[str, list[dict]], *, updated: date
) -> str:
    """1 銘柄ぶんの系列を R2 へ書く。書いたキーを返す。"""
    from .keys import supply_key  # noqa: PLC0415 - 循環 import 回避

    key = supply_key(code)
    existing, _found = store.get_json(key)
    payload = build_payload(
        code, existing=existing, by_type=by_type, updated=updated, writer=store.writer
    )
    store.put_json_guarded(key, payload, contract=CONTRACT)
    return key


__all__ = ["CONTRACT", "SCHEMA_VERSION", "build_payload", "merge_series", "upsert_supply_series"]
