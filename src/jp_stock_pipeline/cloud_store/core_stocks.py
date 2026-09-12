"""①銘柄マスタ `core_stocks` への唯一の書込口（移行 P4a）。

`core_stocks` は移行元 kabulab-cf が所有する既存表で、**14個の子テーブル**が
`stock_id` で参照している（設計書は12個としているが実測は14個）。したがって
うっかり1文でも壊すと影響が広い。この表へ出す SQL は必ずここを通す。

## なぜ upsert を使わないか

`core_stocks` は `name` / `market` が NOT NULL で既定値が無い。そのため
「code と新列だけを渡す部分 upsert」は**既存行が相手でも失敗する**
（実測: `NOT NULL constraint failed: core_stocks.name`。ON CONFLICT の判定より
先に INSERT の NOT NULL 検査が走るため）。よって新列の書込は **UPDATE 一択**。

`D1Store.upsert()` は conflict 以外の全列を `c = excluded.c` に機械展開する
設計なので、この表には使えない（`id` を渡せば `id = excluded.id` が生まれる）。
ここではその関数を一切呼ばない。

## SQL を「組み立てるだけ」にしてある理由

設計書の G-core-1 は「書き込み SQL を実行せずダンプし、サロゲートキー列が
SET 句に一度も現れないことを静的に検査する」ことを求めている。実行と生成が
同じ関数に同居していると、その検査ができない。

## 充填を P4b に置く理由（2026-09-13 追記。実行させる前に必ず読むこと）

`sector33` の出所は決着した（EDINET コードリストの「提出者業種」= commercial-ok。
`collectors/edinet_codelist.py:151`）。つまり `master_sync` はこの値を既に
持っており、Notion ① とローカル ① へは書いている。残っているのは **D1
`core_stocks.sector33` へ UPDATE を流すかどうか**だけである。

本レーンでは流さない。理由は 3 つあり、1 つ目が決定的である。

**(1) 鮮度監視を恒久的に緑にしてしまう。** `cloud_store/datasets.py` の
`core_stocks` は日付列を持たないので、鮮度を `MAX(updated_at)` で測っている。
`build_column_update` は `updated_at = (unixepoch())` を明示的に進めるので、
月次 `master_sync` が 3,818 行を充填すると **`MAX(updated_at)` が毎月必ず
進む**。すると `core_stocks` の SLO（33 日で黄・46 日で赤）は、kabulab-cf の
月次 universe sync が死んでいても発火しない。これは検知したかった事象
（JPX の 404 で銘柄マスタが **33 日**止まったのに誰も気づかなかった）を
自分の書き込みで隠すことになる。設計書 B-8 が「`now` を入れると翌日から
永久に緑」と書いているのと同じ失敗で、**writer 不在より悪い**。
P4b はこれを先に解く必要がある（`src_data_date` / `src_fetched_at` を
充填して鮮度の基準をそちらへ移すか、鮮度 SQL を「kabulab-cf が書いた列だけ」
で測る形にするか）。

**(2) writer 交代と同じ単位の変更になる。** UPDATE を流した瞬間に stockStock は
`core_stocks` の writer になる。`governance.WRITER_CLAIMS` は P4b までこの表を
`kabulab-cf` と宣言しており（そう宣言しないと `universe.ts` の照合が throw して
JPX 母集団同期が止まる）、充填を先に入れると宣言と実態が食い違う。

**(3) 承認とロールバックの単位が違う。** 列追加（DDL・1 回きり）と値の充填
（UPDATE・毎月 3,818 行）で戻し方が違う。G-core-1 の静的検査も「実行しない」
前提で書かれている。
"""

from __future__ import annotations

from .d1 import MAX_COMPOUND_SELECT_TERMS, D1Error

TABLE = "core_stocks"

# 移行元 kabulab-cf が所有する既存列。stockStock は**読むだけ**で書かない。
# `id` はサロゲートキーで 14 子表が参照しており、SET 句に入れたら破滅する。
#
# `updated_at` は意図的に入れていない。`build_column_update` が明示的に進める
# 唯一の既存列なので、ここに入れると自分の UPDATE が自分で弾かれる。
PROTECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "code",
        "name",
        "market",
        "sector",
        "is_active",
        "is_yutai",
        "created_at",
    }
)

# P4a より前から本番 `core_stocks` にある 9 列（2026-09-12 の PRAGMA table_info
# 実測）。`PROTECTED_COLUMNS` + `updated_at` と同じ集合だが、**導出にせず両方を
# リテラルで持つ**。片方を導出にすると、定数から 1 要素を削る変異が両方に同時に
# 効いてテストが追随してしまい検出できない（`tests/test_core_stocks_migrate.py`
# の「実装定数ではなくリテラルで列挙する」と同じ理由）。
# 2 本のリテラルが食い違ったら `tests/test_core_stocks_migrate.py` の
# 突き合わせテストが落ちる。
BASE_COLUMNS: tuple[str, ...] = (
    "id",
    "code",
    "name",
    "market",
    "sector",
    "is_active",
    "is_yutai",
    "created_at",
    "updated_at",
)

# P4a で追加する列。すべて nullable（SQLite の ALTER は既定値の無い NOT NULL も
# UNIQUE も付けられない。実測で両方エラーになることを確認済み）。
NEW_COLUMNS: dict[str, str] = {
    "instrument_type": "TEXT",  # equity/etf/... JPX 由来 → personal-only
    # EDINET コードリストの「提出者業種」(33業種相当) → commercial-ok。
    # 既存の `sector` (JPX 33業種 / personal-only) とは出所が違う別の列である。
    # 値の充填は P4b（理由は docs/CF-CANONICAL-DESIGN.md の P4a 実施記録）。
    "sector33": "TEXT",
    "sector17": "TEXT",  # 17業種 JPX 由来 → personal-only
    "edinet_code": "TEXT",  # EDINETコード → commercial-ok
    "listing_status": "TEXT",  # 上場/監理/整理/上場廃止
    "listing_date": "TEXT",  # YYYY-MM-DD
    "delisting_date": "TEXT",  # YYYY-MM-DD
    "license_tag": "TEXT",  # 行としての代表タグ
    "src_source": "TEXT",  # EDINET / JPX
    "src_data_date": "TEXT",  # データ基準日
    "src_fetched_at": "INTEGER",  # epoch 秒
    "quality": "TEXT",  # 正常/要確認/欠損あり
}

# 追加索引。`idx_core_stocks_edinet` は P4a 時点で全行 NULL なので部分索引にし、
# エントリ0でサイズを食わないようにする。実測で `WHERE edinet_code = ?` が
# COVERING INDEX 走査になることを確認済み。
NEW_INDEXES: dict[str, str] = {
    "idx_core_stocks_active_market": (
        f"CREATE INDEX IF NOT EXISTS idx_core_stocks_active_market"
        f" ON {TABLE} (is_active, market)"
    ),
    "idx_core_stocks_edinet": (
        f"CREATE INDEX IF NOT EXISTS idx_core_stocks_edinet"
        f" ON {TABLE} (edinet_code) WHERE edinet_code IS NOT NULL"
    ),
}


# --- ドリフト検出の期待値（E7）----------------------------------------------
#
# `core_stocks` の列定義は両リポジトリに散っており、**本番の PRAGMA が正**。
# 2026-09-12 実測で 21 列。他の「地図」はすべて古い:
#
#   本番 PRAGMA                                   21 列 ← 正
#   kabulab-cf `src/shared/db/core-schema.ts`      9 列（P4a の 12 列を知らない）
#   kabulab-cf drizzle `0008_snapshot.json`        9 列（同上）
#   stockStock `BASE_COLUMNS` + `NEW_COLUMNS`     21 列 ← ここ
#
# `EXPECTED_COLUMNS` は「stockStock が知っている全列」であり、
# `jobs/core_stocks_migrate.py --verify` が本番 PRAGMA と**両方向**で突き合わせる。
# 片方向（`NEW_COLUMNS ⊆ 本番`）しか見ていなかったため、**本番にあって定義に無い
# 列**（= kabulab-cf 側が勝手に足した列）は検出できていなかった。それが E7 の穴。
EXPECTED_COLUMNS: frozenset[str] = frozenset(BASE_COLUMNS) | frozenset(NEW_COLUMNS)

# P4a より前から本番にある索引（2026-09-12 の sqlite_master 実測）。
BASE_INDEXES: tuple[str, ...] = ("core_stocks_code_unique",)

EXPECTED_INDEXES: frozenset[str] = frozenset(BASE_INDEXES) | frozenset(NEW_INDEXES)

# SQLite が UNIQUE / PRIMARY KEY 制約に対して自動生成する索引。sqlite_master に
# 出るが `sql` が NULL で、こちらが宣言する対象ではない。ドリフト検出の
# 「定義に無い索引」から除く（現行の core_stocks には無いが、kabulab-cf 側が
# 列に UNIQUE を足した瞬間に湧いて誤検知になる）。
AUTOINDEX_PREFIX = "sqlite_autoindex_"


def unexpected_columns(observed: set[str]) -> list[str]:
    """本番にあって stockStock の定義に無い列（E7 の superset 方向）。"""
    return sorted(observed - EXPECTED_COLUMNS)


def unexpected_indexes(observed: set[str]) -> list[str]:
    """本番にあって stockStock の定義に無い索引（自動生成索引は除く）。"""
    return sorted(
        name
        for name in observed - EXPECTED_INDEXES
        if not name.startswith(AUTOINDEX_PREFIX)
    )


def add_column_sql(column: str) -> str:
    """1列分の ALTER。SQLite に `ADD COLUMN IF NOT EXISTS` は無い。"""
    if column not in NEW_COLUMNS:
        raise D1Error(f"P4a の追加対象外の列: {column!r}")
    return f"ALTER TABLE {TABLE} ADD COLUMN {column} {NEW_COLUMNS[column]}"


def plan_ddl(existing_columns: set[str], existing_indexes: set[str]) -> list[str]:
    """まだ適用されていない DDL だけを返す（冪等）。

    `PRAGMA table_info` と `sqlite_master` の実測を渡す。既にある列へ
    `ADD COLUMN` を投げると `duplicate column name` で落ちるため、
    事前に差分を取ってから流す。
    """
    statements = [
        add_column_sql(name) for name in NEW_COLUMNS if name not in existing_columns
    ]
    statements += [
        sql for name, sql in NEW_INDEXES.items() if name not in existing_indexes
    ]
    return statements


def build_column_update(code: str, values: dict[str, object]) -> tuple[str, list]:
    """新列だけを埋める UPDATE を**組み立てて返す**（実行しない）。

    - 既存列（`PROTECTED_COLUMNS`）が1つでも混ざったら `D1Error`
    - P4a の追加対象外の列も `D1Error`
    - `WHERE code = ?` で1行に限定する（`id` は使わない）
    - `updated_at` は明示的に進める。`src_fetched_at`（一次データの取得時刻）
      とは別物なので混同しない
    """
    if not code:
        raise D1Error("build_column_update: code は必須")
    if not values:
        raise D1Error("build_column_update: 更新する列が無い")
    protected = sorted(set(values) & PROTECTED_COLUMNS)
    if protected:
        raise D1Error(
            f"core_stocks の既存列は stockStock から書かない: {protected}"
            "（所有者は kabulab-cf の universe.ts）"
        )
    unknown = sorted(set(values) - set(NEW_COLUMNS))
    if unknown:
        raise D1Error(f"P4a の追加対象外の列: {unknown}")

    columns = sorted(values)
    assignments = ", ".join(f"{c} = ?" for c in columns)
    sql = (
        f"UPDATE {TABLE} SET {assignments}, updated_at = (unixepoch()) WHERE code = ?"
    )
    return sql, [values[c] for c in columns] + [code]


# --- 読み取り（検証用。すべて SELECT / PRAGMA）-------------------------------

TABLE_INFO_SQL = f"PRAGMA table_info({TABLE})"
# `sql` も取る。名前だけ見ていると「同名で別定義の索引が既にある」を
# 素通りさせてしまう（CREATE INDEX IF NOT EXISTS は no-op になる）。
INDEX_LIST_SQL = (
    f"SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='{TABLE}'"
)


def normalize_sql(sql: str | None) -> str:
    """索引定義の比較用。空白の揺れと引用符・IF NOT EXISTS を落とす。"""
    text = " ".join(str(sql or "").split())
    for ch in ("`", '"', "[", "]"):
        text = text.replace(ch, "")
    return text.replace("IF NOT EXISTS ", "").replace(" (", "(").upper()
SNAPSHOT_SQL = (
    f"SELECT id, code, name, market, sector, is_active, is_yutai FROM {TABLE}"
    " ORDER BY id"
)
COUNTS_SQL = (
    f"SELECT COUNT(*) AS total, SUM(is_active) AS active,"
    f" SUM(CASE WHEN sector IS NULL THEN 1 ELSE 0 END) AS sector_null FROM {TABLE}"
)
SEQ_SQL = f"SELECT seq FROM sqlite_sequence WHERE name='{TABLE}'"

# `core_stocks.id` を参照する子表（2026-09-12 実測で14表。設計書の12は誤り）。
# FK 宣言があるものは `stock_id` が NOT NULL。
CHILD_TABLES: tuple[str, ...] = (
    "core_stock_annual_financials",
    "core_stock_financials",
    "ir_disclosures",
    "otakara_stock_financials",
    "otakara_stock_scores",
    "rsi_percentile",
    "swing_daily_ohlcv",
    "swing_entry_signals",
    "swing_stock_indicators",
    "swing_stock_screening",
    "yuho_documents",
    "yuho_order_facts",
    "yuho_overseas_facts",
    "yutai_benefits",
)

# FK 宣言が無い soft 参照。`stock_id` が **nullable** なので、NULL 行を孤児として
# 数えないこと（NULL は「まだ解決していない」であって、実在しない id を指す孤児
# とは別物。混ぜると P5 で jss_financials に行が入った途端に G-core-5 が
# 恒常的に赤くなり、本物の孤児検出が信用されなくなる）。
SOFT_CHILD_TABLES: tuple[str, ...] = ("jss_financials",)

ALL_CHECKED_TABLES: tuple[str, ...] = CHILD_TABLES + SOFT_CHILD_TABLES


def _orphan_term(table: str) -> str:
    """1表ぶんの孤児カウント。soft 参照だけ `stock_id IS NULL` を除外する。"""
    null_guard = " c.stock_id IS NOT NULL AND" if table in SOFT_CHILD_TABLES else ""
    return (
        f"SELECT '{table}' AS t, COUNT(*) AS n FROM {table} c"
        f" LEFT JOIN {TABLE} s ON s.id = c.stock_id"
        f" WHERE{null_guard} s.id IS NULL"
    )


def orphan_check_statements() -> list[str]:
    """全子表の孤児件数を数える文を返す（G-core-5）。

    1文にまとめられない。D1 の compound SELECT は **5 項まで**で、6 項目から
    `too many terms in compound SELECT` を返す（本番実測。素の SQLite の既定
    500 とは大きく違う）。15 表を1文の UNION ALL にすると必ず失敗するので、
    上限ちょうどで分割する。
    """
    chunk = MAX_COMPOUND_SELECT_TERMS
    return [
        " UNION ALL ".join(
            _orphan_term(t) for t in ALL_CHECKED_TABLES[start : start + chunk]
        )
        for start in range(0, len(ALL_CHECKED_TABLES), chunk)
    ]
