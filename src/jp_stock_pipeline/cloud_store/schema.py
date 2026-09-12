"""D1 の jss_* スキーマ (docs/CF-CANONICAL-DESIGN.md §B)。

D1 に置いてよいのは次の3条件を**すべて**満たすものだけ:
(1) 年間増加 10万行以下 (2) 索引でカバーされる述語だけで引ける (3) 1行 2MB 未満。

したがって②日足(年95.5万行)・⑧XBRL全ファクト(年883万行)・需給日次(年210万行)は
ここに**テーブルが存在しない**。それらは R2 の per-code / per-period JSON に置き、
D1 には索引と最新断面だけを持つ。

命名は `jss_` 接頭辞（jp-stock-stock）。既存の core_/swing_/rsi_/otakara_/yutai_/
finmath_/yuho_/ir_ と同居するため、接頭辞で所有を明示する。
"""

from __future__ import annotations

import logging

from ..licensing import LicenseTag

logger = logging.getLogger(__name__)

# ⑤原本の索引。R2 への唯一の入口で、行数が最も多い。
# url と秒精度の取得履歴は R2 のカスタムメタデータに置き、この1行を軽く保つ。
_RAW_FILES = """
CREATE TABLE IF NOT EXISTS jss_raw_files (
  sha256           TEXT PRIMARY KEY,
  r2_bucket        TEXT NOT NULL,
  r2_key           TEXT NOT NULL,
  derived_key      TEXT,
  derived_ext      TEXT,
  source           TEXT NOT NULL,
  datatype         TEXT NOT NULL,
  scope            TEXT NOT NULL,
  doc_id           TEXT,
  code             TEXT,
  data_date        TEXT,
  ext              TEXT NOT NULL,
  size_bytes       INTEGER NOT NULL,
  license_tag      TEXT NOT NULL,
  convert_status   TEXT NOT NULL,
  first_fetched_at INTEGER NOT NULL,
  last_fetched_at  INTEGER NOT NULL
)
"""

# ③財務サマリ。local_store/schema.py の financials と1対1。
# 年次サマリは disclosure_type='本決算' で PK 先頭から索引で引ける。
_FINANCIALS = """
CREATE TABLE IF NOT EXISTS jss_financials (
  code                      TEXT NOT NULL,
  fiscal_period_end         TEXT NOT NULL,
  disclosure_type           TEXT NOT NULL,
  stock_id                  INTEGER,
  consolidated              TEXT,
  accounting_standard       TEXT,
  net_sales                 REAL,
  operating_income          REAL,
  ordinary_income           REAL,
  net_income                REAL,
  eps                       REAL,
  bps                       REAL,
  roe_pct                   REAL,
  roa_pct                   REAL,
  equity_ratio_pct          REAL,
  cf_operating              REAL,
  cf_investing              REAL,
  cf_financing              REAL,
  dps_actual                REAL,
  dps_forecast              REAL,
  forecast_net_sales        REAL,
  forecast_operating_income REAL,
  forecast_ordinary_income  REAL,
  forecast_net_income       REAL,
  forecast_eps              REAL,
  disclosed_at              INTEGER,
  doc_id                    TEXT,
  raw_sha256                TEXT,
  source                    TEXT NOT NULL,
  license_tag               TEXT NOT NULL,
  data_date                 TEXT,
  fetched_at                INTEGER NOT NULL,
  quality                   TEXT NOT NULL,
  PRIMARY KEY (code, fiscal_period_end, disclosure_type)
)
"""

# ⑧ファクトの所在索引。**ファクト本体は1行も D1 に入れない**（年883万行）。
_XBRL_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS jss_xbrl_documents (
  doc_id           TEXT PRIMARY KEY,
  code             TEXT,
  source           TEXT NOT NULL,
  doc_type_code    TEXT,
  period_start     TEXT,
  period_end       TEXT,
  fiscal_year      INTEGER,
  submitted_at     INTEGER,
  fact_count       INTEGER NOT NULL,
  text_block_count INTEGER NOT NULL,
  raw_sha256       TEXT NOT NULL,
  parquet_key      TEXT NOT NULL,
  parquet_bytes    INTEGER NOT NULL,
  license_tag      TEXT NOT NULL
)
"""

# 勘定科目の語彙表。(element, doc_id) の逆引き(年883万ペア)は置かない。
_XBRL_ELEMENTS = """
CREATE TABLE IF NOT EXISTS jss_xbrl_elements (
  element       TEXT PRIMARY KEY,
  namespace     TEXT,
  doc_count     INTEGER NOT NULL,
  first_seen    TEXT,
  last_seen     TEXT,
  is_text_block INTEGER NOT NULL DEFAULT 0
)
"""

# ⑧'需給の最新断面。履歴は R2 supply/{code}.json のみ。
# 空欄・マスク値を 0 に潰さない（NULL のまま。§3-1 推測しない）。
_SUPPLY_LATEST = """
CREATE TABLE IF NOT EXISTS jss_supply_latest (
  code        TEXT NOT NULL,
  data_type   TEXT NOT NULL,
  data_date   TEXT NOT NULL,
  isin        TEXT,
  loan_bal    INTEGER,
  loan_chg    INTEGER,
  stock_bal   INTEGER,
  stock_chg   INTEGER,
  ratio       REAL,
  turn_days   REAL,
  r2_key      TEXT NOT NULL,
  license_tag TEXT NOT NULL DEFAULT 'personal-only',
  fetched_at  INTEGER NOT NULL,
  quality     TEXT NOT NULL,
  PRIMARY KEY (code, data_type)
)
"""

_INDEX_SYMBOLS = """
CREATE TABLE IF NOT EXISTS jss_index_symbols (
  slug         TEXT PRIMARY KEY,
  yahoo_symbol TEXT NOT NULL,
  name_ja      TEXT,
  r2_key       TEXT NOT NULL,
  license_tag  TEXT NOT NULL,
  last_date    TEXT,
  updated_at   INTEGER
)
"""

_JOB_RUNS = """
CREATE TABLE IF NOT EXISTS jss_job_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  job_name      TEXT NOT NULL,
  status        TEXT NOT NULL,
  processed     INTEGER NOT NULL,
  failed        INTEGER NOT NULL,
  failed_codes  TEXT,
  run_url       TEXT,
  duration_secs REAL,
  finished_at   INTEGER NOT NULL
)
"""

# 鮮度の断面。MAX(data_date) の全走査(core 3,764行 / ir 37,338行)を
# 1クエリ20行走査に置き換え、D1 の走査行課金を桁で下げる。
_DATASET_FRESHNESS = """
CREATE TABLE IF NOT EXISTS jss_dataset_freshness (
  dataset             TEXT PRIMARY KEY,
  store               TEXT NOT NULL,
  location            TEXT NOT NULL,
  writer              TEXT NOT NULL,
  latest_data_date    TEXT,
  row_or_object_count INTEGER,
  bytes               INTEGER,
  license_tag         TEXT,
  updated_at          INTEGER NOT NULL
)
"""

# 列単位の writer 排他。ir_disclosures と yutai_benefits は列集合を分割して
# 2 writer を許す必要がある（分類タグと LLM 推定値を消さないため）。
# 各ジョブは冒頭で自分の claim を照合し、想定外の writer なら異常終了する。
_WRITER_CLAIMS = """
CREATE TABLE IF NOT EXISTS jss_writer_claims (
  dataset      TEXT NOT NULL,
  column_group TEXT NOT NULL,
  writer       TEXT NOT NULL,
  updated_at   INTEGER NOT NULL,
  PRIMARY KEY (dataset, column_group)
)
"""

# 列単位のライセンス地図。core_stocks は EDINETコードリスト由来(commercial-ok)と
# JPX data_j.xls 由来(personal-only)が1行に混在するため、行の license_tag 1列では
# 表現できない。公開面のフィルタはこの表を根拠にする。
_COLUMN_LICENSE = """
CREATE TABLE IF NOT EXISTS jss_column_license (
  table_name  TEXT NOT NULL,
  column_name TEXT NOT NULL,
  license_tag TEXT NOT NULL,
  PRIMARY KEY (table_name, column_name)
)
"""

SCHEMA_STATEMENTS: tuple[str, ...] = (
    _RAW_FILES,
    "CREATE INDEX IF NOT EXISTS idx_jss_raw_doc ON jss_raw_files(doc_id)",
    "CREATE INDEX IF NOT EXISTS idx_jss_raw_code_date ON jss_raw_files(code, data_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jss_raw_src_type "
    "ON jss_raw_files(source, datatype, data_date DESC)",
    _FINANCIALS,
    "CREATE INDEX IF NOT EXISTS idx_jss_fin_stock "
    "ON jss_financials(stock_id, fiscal_period_end DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jss_fin_disclosed ON jss_financials(disclosed_at DESC)",
    _XBRL_DOCUMENTS,
    "CREATE INDEX IF NOT EXISTS idx_jss_xbrl_code_period "
    "ON jss_xbrl_documents(code, period_end DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jss_xbrl_fy ON jss_xbrl_documents(fiscal_year, source)",
    _XBRL_ELEMENTS,
    _SUPPLY_LATEST,
    "CREATE INDEX IF NOT EXISTS idx_jss_supply_date "
    "ON jss_supply_latest(data_type, data_date DESC)",
    _INDEX_SYMBOLS,
    _JOB_RUNS,
    "CREATE INDEX IF NOT EXISTS idx_jss_job ON jss_job_runs(job_name, finished_at DESC)",
    _DATASET_FRESHNESS,
    _WRITER_CLAIMS,
    _COLUMN_LICENSE,
)

# 指数・為替の slug は7つ。旧設計の6つでは finmath_daily_ohlcv の7シンボル目
# (日経VI) が欠け、既存画面の入力が1つ失われる。
# nkvi の Yahoo シンボルは未確認のため None にし、確認できるまで収集しない
# （推測で埋めない §3-1）。
INDEX_SYMBOLS: tuple[tuple[str, str | None, str], ...] = (
    ("n225", "^N225", "日経平均株価"),
    ("topix", "^TOPX", "TOPIX"),
    ("vix", "^VIX", "VIX指数"),
    ("gspc", "^GSPC", "S&P500"),
    ("usdjpy", "JPY=X", "米ドル/円"),
    ("niy_f", "NIY=F", "日経225先物(CME円建)"),
    ("nkvi", None, "日経平均ボラティリティー・インデックス"),
)

# 列単位のライセンス。混在するテーブルだけを明示し、それ以外は行の
# license_tag に従う（ここで全列を二重定義しない）。
#
# ## `sector` と `sector33` は**別の出所**である（取り違えると規約違反になる）
#
# 名前が似ているうえに値もほぼ同じなので、1 つの事実の別名だと読んでしまう。
# 実際には writer が違い、したがってライセンスも違う。
#
# | 列 | 書く writer | 一次ソース | タグ |
# |---|---|---|---|
# | `sector`   | kabulab-cf `src/cron/universe.ts`（`sector: r.sector33`） | JPX `data_j.xlsx` の33業種 | **personal-only** |
# | `sector33` | stockStock `master_sync`（値の充填は P4b）                 | EDINET コードリストの「提出者業種」 | **commercial-ok** |
#
# stockStock 側の根拠は `collectors/edinet_codelist.py` の
# `sector33=row[idx[_COL_SECTOR]]`（`_COL_SECTOR = "提出者業種"`）で、レコードの
# Provenance も `Source.EDINET` / `COMMERCIAL_OK` になっている。EDINET 由来の値に
# JPX のタグを貼っていたのが従来の宣言（`sector33` = personal-only）で、これは
# **公開してよい列を公開禁止と宣言していた**誤りである（逆向きの誤りなら漏洩に
# なっていた）。同時に、実際に JPX 由来である `sector` が**地図に一度も載って
# いなかった**。片方だけ直すと「JPX 由来の業種が commercial-ok として公開面に
# 出る」に反転するので、2 列は必ず同時に扱う。
#
# `sector33` は 2026-09-13 時点で本番 3,818 行すべて NULL。タグが公開可に
# なっても**中身が入るのは P4b の充填以降**なので、公開面の業種を
# `sector` → `sector33` へ切り替えるのは充填の後でなければならない
# （切り替えだけ先に入れると業種が全件空欄になる）。
MIXED_LICENSE_COLUMNS: dict[str, dict[str, LicenseTag]] = {
    "core_stocks": {
        # EDINET コードリスト由来 → commercial-ok
        "code": LicenseTag.COMMERCIAL_OK,
        "name": LicenseTag.COMMERCIAL_OK,
        "edinet_code": LicenseTag.COMMERCIAL_OK,
        "sector33": LicenseTag.COMMERCIAL_OK,  # EDINET「提出者業種」。`sector` と混同しない
        # JPX data_j.xlsx 由来 → personal-only
        "market": LicenseTag.PERSONAL_ONLY,
        "sector": LicenseTag.PERSONAL_ONLY,  # kabulab-cf が JPX 33業種を書く既存列
        "sector17": LicenseTag.PERSONAL_ONLY,
        "instrument_type": LicenseTag.PERSONAL_ONLY,
    },
}


def column_license_rows() -> list[list[str]]:
    """jss_column_license へ投入する行を licensing の定義から組み立てる。"""
    rows: list[list[str]] = []
    for table, columns in sorted(MIXED_LICENSE_COLUMNS.items()):
        for column, tag in sorted(columns.items()):
            rows.append([table, column, tag.value])
    return rows


def apply_schema(store) -> int:
    """jss_* を冪等に作成する。作成した文の数を返す。

    D1 は SQLite なので IF NOT EXISTS が効き、既存データは壊さない。
    """
    for statement in SCHEMA_STATEMENTS:
        store.query(statement.strip())
    logger.info("D1 スキーマ適用: %d 文", len(SCHEMA_STATEMENTS))
    return len(SCHEMA_STATEMENTS)


def seed_reference_tables(store) -> None:
    """参照表（指数シンボル・列ライセンス）を投入する。"""
    known = [(slug, sym, name) for slug, sym, name in INDEX_SYMBOLS if sym]
    store.upsert(
        "jss_index_symbols",
        ["slug", "yahoo_symbol", "name_ja", "r2_key", "license_tag"],
        [
            [slug, sym, name, f"index/{slug}.json", LicenseTag.PERSONAL_ONLY.value]
            for slug, sym, name in known
        ],
        conflict=["slug"],
    )
    rows = column_license_rows()
    store.upsert(
        "jss_column_license",
        ["table_name", "column_name", "license_tag"],
        rows,
        conflict=["table_name", "column_name"],
    )
    logger.info(
        "参照表を投入: 指数 %d 件(未確認 %d 件はスキップ) / 列ライセンス %d 件",
        len(known), len(INDEX_SYMBOLS) - len(known), len(rows),
    )


__all__ = [
    "INDEX_SYMBOLS",
    "MIXED_LICENSE_COLUMNS",
    "SCHEMA_STATEMENTS",
    "apply_schema",
    "column_license_rows",
    "seed_reference_tables",
]
