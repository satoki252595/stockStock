"""jss_notion_pages: Notion 行 ID の D1 写し（L-20）。

tdnet_hourly が ① 全 3,846 行を毎時スキャン（39 req × 14 run/日 = 546 req/日）
していたのを、D1 の 1 SELECT に置き換える。書くのは月次の master_sync だけ。
読むのは tdnet_hourly / edinet_daily。

## 写しの区画（`db` 列）

- `"stock_master"`: {銘柄コード: ① page_id}。④ の ① relation 解決用
- `"stock_master_by_edinet"`: {EDINETコード: 銘柄コード}。大量保有報告書は
  対象会社の `secCode` を持たず `issuerEdinetCode` でしか辿れないため、
  逆引きも写す。`page_id` 列に銘柄コードを入れる（4 列に収めるため列を足さない。
  読み手は `load_edinet_code_map` を使い、列の意味を取り違えないこと）

## 古さの扱い

月次更新なので、月初に新規上場した銘柄は次の master_sync まで写しに無い。
読み手は miss を「relation 欠落のみの benign」（`_resolve_master` と同じ扱い）
とし、写し自体が空・表が無いときは Notion スキャンへフォールバックする。
ただし `license_map` が宣言表の存在を検査するので、**本番 DDL（表の作成）を
マージより先に流すこと**（`CREATE TABLE IF NOT EXISTS` は先行して無害）。
"""

from __future__ import annotations

TABLE = "jss_notion_pages"

DB_STOCK_MASTER = "stock_master"
DB_STOCK_MASTER_BY_EDINET = "stock_master_by_edinet"

PARTITIONS: tuple[str, ...] = (DB_STOCK_MASTER, DB_STOCK_MASTER_BY_EDINET)

SELECT_SQL = f"SELECT code, page_id FROM {TABLE} WHERE db = ?"


def load_map(store, db: str) -> dict[str, str]:
    """1 区画を読む。{code: page_id} を返す（L-20 の 1 SELECT）。"""
    return {
        str(r.get("code") or ""): str(r.get("page_id") or "")
        for r in store.query(SELECT_SQL, [db])
    }


def load_stock_master_map(store) -> dict[str, str]:
    """{銘柄コード: ① page_id} を読む。"""
    return load_map(store, DB_STOCK_MASTER)


def load_edinet_code_map(store) -> dict[str, str]:
    """{EDINETコード: 銘柄コード} を読む（大量保有の対象会社解決用）。"""
    return load_map(store, DB_STOCK_MASTER_BY_EDINET)


def save_map(store, db: str, mapping: dict[str, str], *, updated_at: int) -> int:
    """1 区画を upsert する。書いた行数を返す。

    キーに無い行は消さない（upsert のみ）。上場廃止で ① から消えた行の写しが
    残るが、指す page_id が消えるわけではないので relation 解決に害は無い。
    空の mapping は書かない（呼び出し側はマップ取得の成否を見て呼ぶこと。
    失敗時の `{}` で上書きすると写しが空にならないが無駄な往復になる）。
    """
    if db not in PARTITIONS:
        raise ValueError(f"未知の区画: {db!r}")
    if not mapping:
        return 0
    rows = [[code, db, pid, updated_at] for code, pid in sorted(mapping.items())]
    store.upsert(
        TABLE,
        ["code", "db", "page_id", "updated_at"],
        rows,
        conflict=["db", "code"],
    )
    return len(rows)
