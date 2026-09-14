"""③財務サマリ `jss_financials` への書込 SQL の組み立て (移行 P5)。

器は 33 列あるのに本番 0 行で、writer がどちらのリポジトリにも無かった。
`slo.ACCEPTED_RED` の `financials` はその「器だけある」状態の宣言である。

## なぜ `D1Store.upsert()` を使わないか

`D1Store.upsert()` は conflict 以外の**全列を機械的に `c = excluded.c`** へ
展開する。③ ではそれが 2 つの事故を起こす。

1. **訂正開示が完全な行を NULL で潰す。** `jobs/edinet_daily.py` は doc_type_code
   120（有報）と **130（訂正有報）の両方**を `disclosure_type='本決算'` へ落とす
   （同一 PK に着地する）。訂正報告書は訂正した項目だけを載せるのが常で、
   `transform/normalize.tidy_to_financial_record` は決算期末が導出できれば
   残りが None でもレコードを返す。無条件に `excluded.c` を入れると、
   売上だけ訂正した開示が EPS・CF・配当を全部 NULL にする。
   → 値の列は `COALESCE(excluded.c, jss_financials.c)` でマージする。
   「今回運ばれてこなかった項目は前回の値を残す」であって、値の捏造ではない。
   ただし**訂正が本当に値を消した場合**（項目そのものが取り下げられた）と
   「今回は運んでいない」は XBRL からは区別できない。既知の値を残す側へ倒す。
2. **ライセンスの洗浄。** EDINET 由来行(commercial-ok)へ TDnet 由来の訂正
   (factual-cite)が1項目だけ入ると、行の値は混在するのに `license_tag` は
   最後に書いた側になる。逆向き（factual-cite の行へ EDINET が入る）だと
   **厳しいタグが緩いタグに洗われる**。
   → `license_tag` は `licensing._STRICTNESS` と同じ順序を SQL で再現し、
   **厳しい側を残す**。未知のタグは最も厳しい扱いにする（fail-safe）。

## `disclosed_at` ガード

`local_store/mappers.py` の `guard_column="disclosed_at"` と同じ条件
（`excluded >= 既存` または既存が NULL）を `DO UPDATE ... WHERE` に付ける。
古い開示の再取得が新しい訂正を巻き戻さないため。

**ガードが効いて 1 行も更新されなくても SQLite はエラーを返さない。**
したがって `write()` の戻り値は「投げたチャンクの行数」であり、実際に反映
された行数ではない。鮮度の `row_or_object_count` へこの値を流してはいけない
（実表を測る `jobs/freshness_probe.py` が唯一の観測者。writer の自己申告を
鮮度にしない理由は `cloud_store/datasets.py` の冒頭にある）。

## この表に行が入ると日次で **2 経路**が行数ぶん走査する

この writer が動き出した後の継続コストは「表にデータが入ったことの帰結」だが、
走る経路は 1 本ではない。**両方とも `.github/workflows/ops_check.yml` の
同じ日次 cron（`30 14 * * *`）の中**にある。

1. 観測（第1ステップ）: `jobs/freshness_probe.py` が `datasets.py` の
   `SELECT MAX(data_date), MAX(fetched_at), COUNT(*) FROM jss_financials` を撃つ。
   `MAX(data_date)` は無索引なので `EXPLAIN QUERY PLAN` = `SCAN jss_financials`。
2. 孤児検査 G-core-5（第3ステップ、`if: always()`）: `jss_financials` は
   `cloud_store/core_stocks.SOFT_CHILD_TABLES` の唯一の要素なので、
   `core_stocks_migrate --verify` が毎日
   `FROM jss_financials c LEFT JOIN core_stocks s ON s.id = c.stock_id`
   `WHERE c.stock_id IS NOT NULL AND s.id IS NULL` を撃つ。
   `EXPLAIN QUERY PLAN` = `SEARCH c USING COVERING INDEX idx_jss_fin_stock (stock_id>?)`
   および `SEARCH s USING INTEGER PRIMARY KEY (rowid=?)` で、表行ではなく索引エントリだが
   **行数に比例する**。

**この 2 番目を「cron が無いので継続コストではない」と書かないこと。** PR #39 の
初版のコスト申告はそう書いて誤っていた（`jobs/core_stocks_migrate.py` の docstring
自体が「`ops_check.yml` の第3ステップが呼ぶ」と書いてある）。日次の走査は
**行数 × 2** で見積もる。設計上限 100,000 行でも 2 × 100,000 × 30 = 600 万行/月 =
25B rows read/月 allowance の 0.024%。

**索引を足しても減らない。** D1 は被覆されない列のテーブル行フェッチを rows_read に
計上しないので（本リポジトリで対照実験済み）、被覆索引の追加は rows_read の削減に
ならない。減らせるのは「走査そのものを撃たない」形に変えたときだけで、それは
鮮度を開示時刻で測る（= 実表の基準日を見ない）ことになり `cloud_store/slo.py` の
原則に反するため採らない。

## 1 行に 1 つしか持てない来歴

`source` / `fetched_at` / `quality` は NOT NULL なので必ず上書きになる。
つまりこの表は「最後に書いた一次ソース」しか表現できず、値ごとの出自は
持てない。列単位の来歴が要るなら `docs/TARGET-ARCHITECTURE.md` §4.2 の
追記専用（PK に `disclosed_at` を含める）へ進む必要があり、それは断面を読む
側の書き換えを伴うので本レーンの範囲外。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from ..licensing import stricter_tag_sql
from .d1 import MAX_BOUND_PARAMS, D1Error
from .schema import FINANCIALS_PK

if TYPE_CHECKING:
    from ..models import FinancialSummaryRecord
    from .d1 import D1Store

logger = logging.getLogger(__name__)

TABLE = "jss_financials"

# 連結区分が判定できなかったレコードへ入れる明示値。
#
# NULL にはできない。SQLite は PRIMARY KEY 列に NULL を許すうえ `ON CONFLICT` が
# NULL 同士を別物と見るため、nullable な PK 列は「同じキーの行が毎回増える」
# という別の静かな事故になる（2026-09-13 に sqlite 3.51.0 で実測）。
# 詳細は `cloud_store/schema.py` の `_FINANCIALS` のコメント。
UNKNOWN_CONSOLIDATED = "不明"

# `jss_financials` の全 33 列（DDL と同じ順序）。
# 内訳: レコード + Provenance 由来 30 列 + doc_id + raw_sha256 + stock_id。
# `roe_pct` / `roa_pct` は `transform/normalize.py` が常に None を入れる
# （短信サマリに直接出るときだけ将来対応。計算で補わない §3-1）。
COLUMNS: tuple[str, ...] = (
    "code",
    "fiscal_period_end",
    "disclosure_type",
    "stock_id",
    "consolidated",
    "accounting_standard",
    "net_sales",
    "operating_income",
    "ordinary_income",
    "net_income",
    "eps",
    "bps",
    "roe_pct",
    "roa_pct",
    "equity_ratio_pct",
    "cf_operating",
    "cf_investing",
    "cf_financing",
    "dps_actual",
    "dps_forecast",
    "forecast_net_sales",
    "forecast_operating_income",
    "forecast_ordinary_income",
    "forecast_net_income",
    "forecast_eps",
    "disclosed_at",
    "doc_id",
    "raw_sha256",
    "source",
    "license_tag",
    "data_date",
    "fetched_at",
    "quality",
)

# NOT NULL の来歴列。COALESCE しても必ず excluded 側が残るので明示的に上書きする。
OVERWRITE_COLUMNS: tuple[str, ...] = ("source", "fetched_at", "quality")

# 厳しい側を残す列。
LICENSE_COLUMN = "license_tag"

# 巻き戻し防止に使う列。
GUARD_COLUMN = "disclosed_at"

# 上の3分類に入らない列は COALESCE でマージする（訂正開示の NULL 潰し対策）。
MERGE_COLUMNS: tuple[str, ...] = tuple(
    c
    for c in COLUMNS
    if c not in FINANCIALS_PK
    and c not in OVERWRITE_COLUMNS
    and c != LICENSE_COLUMN
)

# `core_stocks.code` は UNIQUE 索引 (`core_stocks_code_unique`) があるので
# 索引 1 エントリで解決でき、走査行は 1 行で済む。
STOCK_ID_SQL = "SELECT id FROM core_stocks WHERE code = ? LIMIT 1"

COUNT_SQL = f"SELECT COUNT(*) AS n FROM {TABLE}"

# 1 リクエストで引くコード数。バインド上限 100 に少し余裕を残す。
STOCK_ID_BATCH = 90


def build_upsert_sql(row_count: int) -> str:
    """`row_count` 行ぶんの INSERT ... ON CONFLICT DO UPDATE を組み立てる。

    `cloud_store/core_stocks.py` と同じく**実行はしない**。SQL を生成する関数を
    分けておくと、発行前の文をテストで直接検査できる。
    """
    if row_count <= 0:
        raise D1Error("financials: 0 行の upsert は組み立てない")
    placeholders = "(" + ", ".join("?" for _ in COLUMNS) + ")"
    assignments = [
        f"{c} = COALESCE(excluded.{c}, {TABLE}.{c})" for c in MERGE_COLUMNS
    ]
    assignments += [f"{c} = excluded.{c}" for c in OVERWRITE_COLUMNS]
    assignments.append(
        f"{LICENSE_COLUMN} = "
        + stricter_tag_sql(f"excluded.{LICENSE_COLUMN}", f"{TABLE}.{LICENSE_COLUMN}")
    )
    return (
        f"INSERT INTO {TABLE} ({', '.join(COLUMNS)})"
        f" VALUES {', '.join(placeholders for _ in range(row_count))}"
        f" ON CONFLICT ({', '.join(FINANCIALS_PK)}) DO UPDATE SET"
        f" {', '.join(assignments)}"
        f" WHERE excluded.{GUARD_COLUMN} >= {TABLE}.{GUARD_COLUMN}"
        f" OR {TABLE}.{GUARD_COLUMN} IS NULL"
    )


def _epoch(value: datetime | None) -> int | None:
    return int(value.timestamp()) if value is not None else None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def record_to_row(
    record: "FinancialSummaryRecord",
    *,
    stock_id: int | None,
    doc_id: str | None,
    raw_sha256: str | None,
) -> list[Any]:
    """レコードを `COLUMNS` の順で 1 行に並べる。

    `fiscal_period_end` / `data_date` は TEXT 列なので ISO 文字列、
    `disclosed_at` / `fetched_at` は INTEGER 列なので epoch 秒へ落とす。
    """
    if not record.code:
        raise D1Error("financials: code が空のレコードは PK を構成できない")
    if record.fiscal_period_end is None:
        raise D1Error("financials: fiscal_period_end が無いレコードは書かない")
    prov = record.provenance
    values: dict[str, Any] = {
        "code": record.code,
        "fiscal_period_end": _iso(record.fiscal_period_end),
        "disclosure_type": record.disclosure_type,
        "stock_id": stock_id,
        "consolidated": record.consolidated or UNKNOWN_CONSOLIDATED,
        "accounting_standard": record.accounting_standard,
        "net_sales": record.net_sales,
        "operating_income": record.operating_income,
        "ordinary_income": record.ordinary_income,
        "net_income": record.net_income,
        "eps": record.eps,
        "bps": record.bps,
        "roe_pct": record.roe_pct,
        "roa_pct": record.roa_pct,
        "equity_ratio_pct": record.equity_ratio_pct,
        "cf_operating": record.cf_operating,
        "cf_investing": record.cf_investing,
        "cf_financing": record.cf_financing,
        "dps_actual": record.dps_actual,
        "dps_forecast": record.dps_forecast,
        "forecast_net_sales": record.forecast_net_sales,
        "forecast_operating_income": record.forecast_operating_income,
        "forecast_ordinary_income": record.forecast_ordinary_income,
        "forecast_net_income": record.forecast_net_income,
        "forecast_eps": record.forecast_eps,
        "disclosed_at": _epoch(record.disclosed_at),
        "doc_id": doc_id,
        "raw_sha256": raw_sha256,
        "source": prov.source.value,
        "license_tag": prov.license_tag.value,
        "data_date": _iso(prov.data_date),
        "fetched_at": _epoch(prov.fetched_at),
        "quality": prov.quality.value,
    }
    missing = [c for c in COLUMNS if c not in values]
    if missing:
        raise D1Error(f"financials: 値を組み立てられない列がある: {missing}")
    return [values[c] for c in COLUMNS]


def resolve_stock_id(
    store: "D1Store", code: str, *, cache: dict[str, int | None] | None = None
) -> int | None:
    """`core_stocks.id` を引く。見つからなければ None（孤児にはしない）。

    `core_stocks.stock_id` を持たない ETF・優先株は実在する（`jss_supply_latest`
    の distinct code 4,351 件中 592 件が `core_stocks` に無い）。したがって
    未解決は異常ではなく NULL が正しい。`cloud_store/core_stocks.py` の孤児検査は
    soft 参照の NULL を除外しているので、NULL 行が G-core-5 を赤くすることも無い。

    `cache` を渡すと 1 ジョブ実行内の同一コードの再問い合わせを省ける
    （D1 は走査行課金なので往復そのものより行数が効くが、1 銘柄が同日に複数の
    書類を出すのは普通なので効く）。
    """
    if not code:
        return None
    if cache is not None and code in cache:
        return cache[code]
    rows = store.query(STOCK_ID_SQL, [code])
    stock_id = int(rows[0]["id"]) if rows and rows[0].get("id") is not None else None
    if cache is not None:
        cache[code] = stock_id
    return stock_id


def prefetch_stock_ids(
    store: "D1Store", codes: list[str], *, cache: dict[str, int | None]
) -> int:
    """複数コードの `core_stocks.id` を **1 リクエストにまとめて**解決する。

    往復回数を減らすためだけの最適化で、**走査行は増えない**（`code IN (...)` も
    `core_stocks_code_unique` の索引エントリ引きなので、引いたコード数と同じ
    行数しか読まない）。D1 の課金軸は走査行なので、
    `SELECT id, code FROM core_stocks` を丸ごと読む「全件マップ」は採らない
    （4,445 行 × 毎時 11 回 = 48,895 行/日 になり、必要なのが 61 件でも払う）。

    `tdnet_hourly` は同一日の一覧を毎時まるごと再処理するので、繁忙日
    （短信 1,000 件超）の 1 回の実行で per-code SELECT が 1,000 往復になる。
    本番実測の往復 1 回 ≒ 202 ms（4,755 行を 1 行ずつで約 16 分）だと 3.4 分で、
    30 分 cap の同ジョブにそのまま乗る。12 リクエストへ畳めば 2.4 秒で済む。

    戻り値は問い合わせたコード数（キャッシュに載っていたものを除く）。
    """
    wanted = sorted({c for c in codes if c and c not in cache})
    if not wanted:
        return 0
    for start in range(0, len(wanted), STOCK_ID_BATCH):
        chunk = wanted[start : start + STOCK_ID_BATCH]
        if len(chunk) > MAX_BOUND_PARAMS:  # pragma: no cover - 定数で保証済み
            raise D1Error(f"financials: バインド上限超過 {len(chunk)}")
        placeholders = ", ".join("?" for _ in chunk)
        rows = store.query(
            f"SELECT id, code FROM core_stocks WHERE code IN ({placeholders})",
            list(chunk),
        )
        found = {
            str(r["code"]): int(r["id"])
            for r in rows
            if r.get("id") is not None and r.get("code") is not None
        }
        for code in chunk:
            # 見つからなかったコードも None で覚える。覚えないと per-code SELECT へ
            # 落ちて、まとめた意味が無くなる（ETF・優先株は実在する）。
            cache[code] = found.get(code)
    return len(wanted)


def write(store: "D1Store", rows: list[list[Any]]) -> int:
    """行を D1 へ流す。**投げたチャンクの行数**を返す（反映行数ではない）。

    D1 のバインドパラメータ上限 100 から 33 列 → 3 行/リクエスト。
    compound SELECT の 5 項上限（`d1.MAX_COMPOUND_SELECT_TERMS`）は UNION を
    含む文にしか効かないので、複数行 VALUES のこの文には掛からない。
    """
    if not rows:
        return 0
    for index, row in enumerate(rows):
        if len(row) != len(COLUMNS):
            raise D1Error(
                f"financials: {index} 行目の値の数が列数と違う:"
                f" {len(row)} != {len(COLUMNS)}"
            )
    chunk_size = store.rows_per_request(len(COLUMNS))
    written = 0
    for start in range(0, len(rows), chunk_size):
        chunk = rows[start : start + chunk_size]
        params: list[Any] = []
        for row in chunk:
            params.extend(row)
        store.query(build_upsert_sql(len(chunk)), params)
        written += len(chunk)
    return written


def count_rows(store: "D1Store") -> int | None:
    """実際に入っている行数。読めなければ None。

    `write()` の戻り値と混同しないために別関数にしてある。鮮度の観測は
    `jobs/freshness_probe.py` が実表を測るので、ここは運用確認用。
    """
    rows = store.query(COUNT_SQL)
    if not rows:
        return None
    value = rows[0].get("n")
    return int(value) if value is not None else None


__all__ = [
    "COLUMNS",
    "GUARD_COLUMN",
    "LICENSE_COLUMN",
    "MERGE_COLUMNS",
    "OVERWRITE_COLUMNS",
    "STOCK_ID_BATCH",
    "STOCK_ID_SQL",
    "TABLE",
    "UNKNOWN_CONSOLIDATED",
    "build_upsert_sql",
    "count_rows",
    "prefetch_stock_ids",
    "record_to_row",
    "resolve_stock_id",
    "write",
]
