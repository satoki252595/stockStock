"""①銘柄マスタ `core_stocks` の列定義の地図と検証用 SQL（移行 P4a 適用済み）。

`core_stocks` は移行元 kabulab-cf が所有する既存表で、**14個の子テーブル**が
`stock_id` で参照している（設計書は12個としているが実測は14個）。P4a の列追加
（12 列 + 2 索引）は適用済みで、DDL 発行コード（`--apply` / `plan_ddl` /
`build_column_update`）は削除した（D-14-1）。残る stockStock の書込は
`sector33` の充填（`build_sector33_updates`）だけである。

`D1Store.upsert()` は conflict 以外の全列を `c = excluded.c` に機械展開する
設計なので、この表には使えない（`id` を渡せば `id = excluded.id` が生まれる）。
`name` / `market` が NOT NULL で既定値が無いため「code と新列だけの部分
upsert」も既存行相手に失敗する（実測: `NOT NULL constraint failed:
core_stocks.name`）。書込は UPDATE 一択で、ここではその関数を一切呼ばない。

## sector33 の充填で updated_at を進めない理由（2026-09-13。書き換える前に必ず読むこと）

`sector33` の出所は決着している（EDINET コードリストの「提出者業種」=
commercial-ok。`collectors/edinet_codelist.py` の `_COL_SECTOR`）。2026-09-13 に
ユーザが「公開面の業種を33業種で表示する」と決め、公開面（kabulab-cf
`src/shared/db/public-columns.ts`）は既に `core_stocks.sector33` を読んでいる。
本番は全行 NULL で「—」表示だったので、`master_sync` がこの列を埋める。

当初この充填を P4b に置いた理由は 3 つあった。それぞれ次のように解いた。

**(1) 鮮度監視を恒久的に緑にしてしまう → `updated_at` を SET しない専用の
UPDATE で解く。** `cloud_store/datasets.py` の `core_stocks` は日付列を持たない
ので、鮮度を `MAX(updated_at)` で測っている。汎用の列充填（D-14-1 で削除）は
`updated_at = (unixepoch())` を明示的に進めるので、これで月次に充填すると
**`MAX(updated_at)` が毎月必ず進み**、kabulab-cf の月次 universe sync が死んで
いても `core_stocks` の SLO（33 日で黄・46 日で赤）が発火しない（JPX の 404 で
銘柄マスタが 33 日止まったのに誰も気づかなかった事象を、自分の書き込みで隠す。
設計書 B-8 の「`now` を入れると永久に緑」と同型）。
そこで `build_sector33_updates` は `sector33` だけを SET する。`updated_at` に
触るのは kabulab-cf の `universe.ts` だけなので、鮮度の意味は変わらない。
汎用の列充填（旧 `build_column_update`）は D-14-1 で削除した。
SQL に `updated_at` が現れないことは `tests/test_core_stocks_sector33.py` が固定する。
本番 `core_stocks` に trigger が無いこと（UPDATE が裏で `updated_at` を進めない
こと）は 2026-09-13 に `sqlite_master` で確認した。

採らなかった案: `src_fetched_at` を同時に埋めて鮮度の基準をそちらへ移す。
EDINET 側の取得時刻で測ると「kabulab-cf の同期が止まった」を検知できなくなり、
解きたい問題が別の形で戻る。

**(2) writer 交代と同じ単位の変更 → 同一 PR で claim を足す。**
`governance.WRITER_CLAIMS` に `core_stocks/enrich = stockStock` を足す。
`base` 群（行の作成と既存列）の writer は `kabulab-cf` のまま。kabulab-cf の
`universe.ts` は `sector33` を SET せず（`sector` は JPX 由来の別列）、2026-09-13
時点で claim の照合コードも持たないので、この宣言で JPX 母集団同期は止まらない。

**(3) 承認とロールバックの単位 → 2026-09-13 にユーザ承認。** 戻し方は
`UPDATE core_stocks SET sector33 = NULL`（`updated_at` を進めない）と
`jss_writer_claims` の `('core_stocks', 'enrich')` 行の削除。`instrument_type` は
kabulab-cf #27（P4b 第 1 段）から `universe.ts` が埋め、claim は `base` に数える
（`governance` の注記）。残りの P4a 列の充填は引き続き P4b。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ..contracts.sector33 import normalize_sector33
from ..contracts.stock_code import source_code_to_ticker
from .d1 import MAX_BOUND_PARAMS, MAX_COMPOUND_SELECT_TERMS

TABLE = "core_stocks"

# 移行元 kabulab-cf が所有する既存列。stockStock は**読むだけ**で書かない。
# `id` はサロゲートキーで 14 子表が参照しており、SET 句に入れたら破滅する。
# `build_sector33_updates` が既存列を SET しないことは
# `tests/test_core_stocks_sector33.py` が固定する。
#
# `updated_at` は意図的に入れていない。kabulab-cf の `universe.ts` が進める
# 列で、stockStock 側の充填が触らないことを別途固定している。
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

# P4a で追加した列（適用済み）。すべて nullable（SQLite の ALTER は既定値の無い
# NOT NULL も UNIQUE も付けられない。実測で両方エラーになることを確認済み）。
NEW_COLUMNS: dict[str, str] = {
    "instrument_type": "TEXT",  # equity/etf/... JPX 由来 → personal-only
    # EDINET コードリストの「提出者業種」(33業種相当) → commercial-ok。
    # 既存の `sector` (JPX 33業種 / personal-only) とは出所が違う別の列である。
    # 値は master_sync が東証33業種の名称へ正規化して埋める
    # （`build_sector33_updates`。updated_at を進めない）。
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


# --- sector33 の充填（EDINET「提出者業種」）----------------------------------
#
# 専用の UPDATE を持つ理由は、モジュール docstring の「sector33 の充填で
# updated_at を進めない理由」を読むこと。要点だけ書くと、`core_stocks` の
# 鮮度は `MAX(updated_at)` で測っているので、月次の充填が `updated_at` を
# 進めると kabulab-cf の universe sync が死んでも SLO が鳴らない。

SECTOR33_COLUMN = "sector33"

# 差分を取るための読み取り。全行（約 3,818 行）を 1 文で読む。月 1 回なので
# rows_read は月 3,818 行。`WHERE sector33 IS NOT ...` で絞る案は、コードリストと
# 比べるまで「変わったか」が分からないので成立しない。
SECTOR33_SNAPSHOT_SQL = f"SELECT code, {SECTOR33_COLUMN} FROM {TABLE}"


def plan_sector33_updates(
    current_rows: Iterable[Mapping[str, object]],
    codelist: Iterable[tuple[str | None, str | None]],
) -> dict[str, str | None]:
    """書くべき `{code: 新しい sector33}` を返す（値が変わる行だけ）。

    - `current_rows` は `SECTOR33_SNAPSHOT_SQL` の結果（D1 側の正）
    - `codelist` は `(証券コード, 提出者業種)` の組。コードは `source_code_to_ticker`
      で 4 文字にする（5 文字は末尾 "0" のときだけ。別証券への取り違えを避ける）
    - **コードリストに現れない銘柄は触らない。** コードリストの一時的な欠落や
      REIT・インフラファンド（EDINET に証券コードが無い）で既存値を NULL に
      潰さない。NULL を書くのは「コードリストに居て、正規化結果が None」の銘柄だけ
    - 同じティッカーが 2 回現れたら後勝ち（`master_sync._dedup_by_code` と同じ）
    """
    desired: dict[str, str | None] = {}
    for raw_code, raw_sector in codelist:
        ticker = source_code_to_ticker(raw_code)
        if ticker is None:
            continue
        desired[ticker] = normalize_sector33(raw_sector)

    changes: dict[str, str | None] = {}
    for row in current_rows:
        code = str(row.get("code") or "")
        if code not in desired:
            continue
        new = desired[code]
        if row.get(SECTOR33_COLUMN) != new:
            changes[code] = new
    return changes


def build_sector33_updates(changes: Mapping[str, str | None]) -> list[tuple[str, list]]:
    """`sector33` **だけ**を SET する UPDATE 文の列を**組み立てて返す**（実行しない）。

    形は `UPDATE core_stocks SET sector33 = ? WHERE code IN (?, ...)` で、同じ値を
    書く銘柄を 1 文にまとめ、1 文のバインド数を `MAX_BOUND_PARAMS` 以下に切る。

    - `updated_at` を SET しない（鮮度監視を自分の書き込みで隠さないため）
    - サロゲートキー `id` も既存列も SET しない（G-core-1）
    - `WHERE code IN` は `core_stocks_code_unique` 索引で引けるので、走査行は
      書く行数と同じオーダーに収まる

    採らなかった案: `SET sector33 = CASE code WHEN ? THEN ? ... END WHERE code IN
    (...)`。1 行あたり 3 バインドで 1 文 33 行になり、初回の約 3,704 行で 113 文。
    値でまとめる形なら 34 値 × 99 行で約 50 文に減る。加えて CASE は WHEN と IN の
    列が 1 つでもずれると ELSE 無しで NULL を書く（既存値を黙って潰す）が、
    値でまとめる形はその失敗の仕方を持たない。
    """
    by_value: dict[str | None, list[str]] = {}
    for code in sorted(changes):
        by_value.setdefault(changes[code], []).append(code)

    per_statement = MAX_BOUND_PARAMS - 1  # 先頭の 1 個は SET の値
    statements: list[tuple[str, list]] = []
    # None を先頭に置いて並びを決定的にする（dict の挿入順に依存させない）
    for value in sorted(by_value, key=lambda v: (v is not None, v or "")):
        codes = by_value[value]
        for start in range(0, len(codes), per_statement):
            chunk = codes[start : start + per_statement]
            placeholders = ", ".join("?" for _ in chunk)
            sql = (
                f"UPDATE {TABLE} SET {SECTOR33_COLUMN} = ?"
                f" WHERE code IN ({placeholders})"
            )
            statements.append((sql, [value, *chunk]))
    return statements


# --- 読み取り（検証用。すべて SELECT / PRAGMA）-------------------------------

TABLE_INFO_SQL = f"PRAGMA table_info({TABLE})"
# `sql` も取る。名前だけ見ていると「同名で別定義の索引が既にある」を
# 素通りさせてしまう。
INDEX_LIST_SQL = (
    f"SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='{TABLE}'"
)

# `core_stocks.id` を参照する子表（2026-09-12 実測で14表。設計書の12は誤り。
# L-52 で `swing_stock_screening` を抜いて 13 表）。
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

# FK 宣言を持たないが `stock_id` でぶら下がる表（宣言が無いので DB が守らない）。
# `p_momentum.stock_id` / `p_yuho_growth.stock_id` は PRIMARY KEY（NOT NULL）のため
# NULL 除外は要らない。本番 sqlite_master で `stock_id` を持つ 16 表
# （CHILD 13 + SOFT 1 + ここ 2）を確認（p_yuho_growth は P4 適用後）。
NOFK_CHILD_TABLES: tuple[str, ...] = ("p_momentum", "p_yuho_growth")

ALL_CHECKED_TABLES: tuple[str, ...] = CHILD_TABLES + SOFT_CHILD_TABLES + NOFK_CHILD_TABLES

# 日次 `--verify` で回す表。FK 宣言のある 14 表は `PRAGMA foreign_keys=1` の
# 本番 D1 が INSERT 時に弾くので毎日走査しない（L-16。日次 −約 95 万 rows_read）。
# 宣言の無い 2 表だけは DB が守らないので毎日数える。
DAILY_CHECK_TABLES: tuple[str, ...] = SOFT_CHILD_TABLES + NOFK_CHILD_TABLES

FOREIGN_KEYS_PRAGMA = "PRAGMA foreign_keys"


def _orphan_term(table: str) -> str:
    """1表ぶんの孤児カウント。soft 参照だけ `stock_id IS NULL` を除外する。"""
    null_guard = " c.stock_id IS NOT NULL AND" if table in SOFT_CHILD_TABLES else ""
    return (
        f"SELECT '{table}' AS t, COUNT(*) AS n FROM {table} c"
        f" LEFT JOIN {TABLE} s ON s.id = c.stock_id"
        f" WHERE{null_guard} s.id IS NULL"
    )


def _chunked(tables: tuple[str, ...]) -> list[str]:
    chunk = MAX_COMPOUND_SELECT_TERMS
    return [
        " UNION ALL ".join(_orphan_term(t) for t in tables[start : start + chunk])
        for start in range(0, len(tables), chunk)
    ]


def orphan_check_statements() -> list[str]:
    """全子表の孤児件数を数える文を返す（G-core-5）。

    1文にまとめられない。D1 の compound SELECT は **5 項まで**で、6 項目から
    `too many terms in compound SELECT` を返す（本番実測。素の SQLite の既定
    500 とは大きく違う）。16 表を1文の UNION ALL にすると必ず失敗するので、
    上限ちょうどで分割する。
    """
    return _chunked(ALL_CHECKED_TABLES)


def daily_orphan_check_statements() -> list[str]:
    """日次 `--verify` 用。宣言の無い表（SOFT + NOFK）だけを数える（L-16）。"""
    return _chunked(DAILY_CHECK_TABLES)
