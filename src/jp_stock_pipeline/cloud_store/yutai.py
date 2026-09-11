"""⑨優待の LLM 派生値を R2 へ退避する (移行 P3)。

kabulab-cf の D1 `yutai_benefits` は、出典サイト(みんかぶ)の掲載文
`description` と、そこから機械/LLM で導いた `short_summary` /
`estimated_value` を持つ。⑨優待は TDnet の一次開示から作り直す方針だが、
**派生値は作り直せない**（出典サイトの規約が再取得を禁じており、元の掲載文を
引き直せない）。作り直しが終わるまでの保険として、派生値だけを退避する。

## 退避しないもの

`description` は出典サイトの掲載文そのもので、規約が蓄積・再掲を禁じている。
**SQL の選択列に入れない**（`EXPORT_COLUMNS` / テストで固定）。

## 置き場所

`jp-stock-supply`。personal-only のため、公開 Worker が bind していない
バケットに置いて物理的に到達不能にする（第0層の防御。`worker/src/public.ts`
と同じ考え方）。`jp-stock-raw` は公開 Worker が bind しているので使わない。

## 重複の回避

キーは内容の SHA256 だけで決まる (`snapshot_key`)。同じ中身を何度流しても
同じキーになり、2つ目のオブジェクトは作られない。日付をキーにも本文にも
入れないのはこのため（取得時刻は索引側の項目に持つ）。
"""

from __future__ import annotations

import json
from typing import Any

from .guards import GuardError
from .keys import SHA_PREFIX_LEN

# 退避する列。`description` は**入れてはいけない**（出典掲載文そのもの）。
EXPORT_COLUMNS: tuple[str, ...] = (
    "id",
    "code",
    "genre",
    "min_shares",
    "record_month",
    "short_summary",
    "estimated_value",
)

# 退避元の SELECT。列を足すときは EXPORT_COLUMNS と test_cloud_yutai を必ず揃える。
EXPORT_SQL = (
    "SELECT b.id AS id, s.code AS code, g.name AS genre,"
    " b.min_shares AS min_shares, b.record_month AS record_month,"
    " b.short_summary AS short_summary, b.estimated_value AS estimated_value"
    " FROM yutai_benefits b"
    " JOIN core_stocks s ON s.id = b.stock_id"
    " LEFT JOIN yutai_genres g ON g.id = b.genre_id"
    " WHERE b.id > ? ORDER BY b.id LIMIT ?"
)

COUNT_SQL = (
    "SELECT COUNT(*) AS rows,"
    " SUM(CASE WHEN estimated_value IS NOT NULL THEN 1 ELSE 0 END) AS with_value,"
    " SUM(CASE WHEN short_summary IS NOT NULL AND TRIM(short_summary) <> ''"
    " THEN 1 ELSE 0 END) AS with_summary"
    " FROM yutai_benefits"
)

# 1 リクエストで引く行数。D1 の応答サイズ上限に余裕を持たせる。
PAGE_SIZE = 1000

# 2026-09-12 に実測した基準値。これを大きく下回るのは元データの破損を疑う。
BASELINE_ROWS = 8314
BASELINE_WITH_VALUE = 5333
BASELINE_WITH_SUMMARY = 8314

# 既定の許容下限比。優待廃止で自然に減ることはあるため 1.0 にはしない。
DEFAULT_MIN_RATIO = 0.9

SCHEMA_ID = "kabulab.yutai_benefits/1"
INDEX_KEY = "backup/yutai/index.json"

SNAPSHOT_CONTRACT: dict[str, tuple[str, ...]] = {
    "$": ("schema", "counts", "rows"),
    "rows[]": EXPORT_COLUMNS,
}
INDEX_CONTRACT: dict[str, tuple[str, ...]] = {
    "$": ("snapshots",),
    "snapshots[]": ("taken_at", "key", "sha256", "counts"),
}


def snapshot_key(sha256: str) -> str:
    """内容だけで決まる immutable キー。同一内容の再実行で重複を作らない。"""
    if not sha256:
        raise ValueError("snapshot_key: sha256 は必須")
    return f"backup/yutai/{sha256[:SHA_PREFIX_LEN]}.json"


def serialize(payload: dict[str, Any]) -> bytes:
    """SHA256 が内容だけで決まるよう決定的に直列化する。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def fetch_rows(query, *, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
    """`yutai_benefits` を id のキーセット走査で全件引く。

    OFFSET は行数に比例して遅くなるうえ、走査行数課金の D1 では単純に高くつく。
    id の索引(主キー)だけで進めるため `WHERE id > ?` で辿る。
    """
    rows: list[dict[str, Any]] = []
    last_id = 0
    while True:
        page = query(EXPORT_SQL, [last_id, page_size])
        if not page:
            break
        rows.extend(page)
        last_id = int(page[-1]["id"])
        if len(page) < page_size:
            break
    return rows


def count_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    """退避した行から件数を数える（元 DB の COUNT と突き合わせる材料）。"""
    return {
        "rows": len(rows),
        "with_value": sum(1 for r in rows if r.get("estimated_value") is not None),
        "with_summary": sum(
            1 for r in rows if str(r.get("short_summary") or "").strip()
        ),
    }


def check_floors(
    counts: dict[str, int],
    *,
    min_ratio: float = DEFAULT_MIN_RATIO,
    previous: dict[str, int] | None = None,
    allow_shrink: bool = False,
) -> None:
    """件数が基準値・前回を下回っていないか。下回ったら退避を中止する。

    退避先のキーは内容ハッシュなので上書きは起きないが、「壊れた状態を
    正しい退避として索引に載せる」ことは防ぐ必要がある (§3-2 欠損を隠さない)。
    """
    baseline = {
        "rows": BASELINE_ROWS,
        "with_value": BASELINE_WITH_VALUE,
        "with_summary": BASELINE_WITH_SUMMARY,
    }
    for name, base in baseline.items():
        floor = int(base * min_ratio)
        actual = counts.get(name, 0)
        if actual < floor:
            raise GuardError(
                f"⑨優待の {name} が基準を下回った: {actual} < {floor}"
                f"（基準 {base} × {min_ratio}）。元データの破損を疑うため退避しない"
            )
    if previous and not allow_shrink:
        for name in baseline:
            before = previous.get(name, 0)
            actual = counts.get(name, 0)
            if actual < before:
                raise GuardError(
                    f"⑨優待の {name} が前回退避より減った: {before} -> {actual}。"
                    " 意図した減少なら --allow-shrink を付けて実行する"
                )


def build_snapshot(
    rows: list[dict[str, Any]], *, source_db: str, counts: dict[str, int]
) -> dict[str, Any]:
    """退避本文を組み立てる。**取得時刻は入れない**（内容ハッシュを安定させる）。"""
    leaked = set(rows[0]) - set(EXPORT_COLUMNS) if rows else set()
    if leaked:
        raise GuardError(f"退避対象外の列が混ざっている: {sorted(leaked)}")
    return {
        "schema": SCHEMA_ID,
        "source_db": source_db,
        "license_tag": "personal-only",
        "counts": counts,
        "rows": sorted(rows, key=lambda r: int(r["id"])),
    }


def latest_entry(index: Any) -> dict[str, Any] | None:
    """索引の最新エントリ。索引が壊れている/空なら None。"""
    if not isinstance(index, dict):
        return None
    snapshots = index.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return None
    last = snapshots[-1]
    return last if isinstance(last, dict) else None


def append_index(index: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """索引に1件足した新しい索引を返す（既存要素は落とさない）。"""
    snapshots = list(index.get("snapshots") or []) if isinstance(index, dict) else []
    snapshots.append(entry)
    base = dict(index) if isinstance(index, dict) else {}
    base["snapshots"] = snapshots
    return base
