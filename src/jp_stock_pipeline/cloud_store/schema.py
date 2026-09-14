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
from collections.abc import Iterable
from dataclasses import dataclass

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
#
# ## PK に `consolidated` が要る理由（C1 と同じ後勝ちを作らないため）
#
# 旧 PK は `(code, fiscal_period_end, disclosure_type)` で `consolidated` が
# 非キー列だった。決算短信・有報は**同一期末の連結と単体を両方載せる**ので、
# この PK では後に書いた方が前の行を潰す。それは `core_stock_annual_financials`
# で現に起きている事故と同じ構造である（Yahoo が連結と単体を混在させ、持株会社
# 464 銘柄中 296 が自系列内で 10 倍超のスパンを持つ）。連結と単体は同じ期の
# **別の測定範囲**であって、どちらかが正しいのではなく両方が必要な事実なので、
# キーで分ける以外に表現する手が無い。`docs/TARGET-ARCHITECTURE.md` §4.2 も
# あるべきキーに `consolidated` を含めている。
#
# ## `consolidated` を NOT NULL にした理由（SQLite の NULL 重複許容）
#
# SQLite は **PRIMARY KEY 列に NULL を許す**（INTEGER PRIMARY KEY / WITHOUT ROWID
# / STRICT / 明示 NOT NULL を除く）。2026-09-13 に sqlite 3.51.0 で実測:
# nullable な PK 列へ NULL を 2 回 INSERT すると 2 行できてしまい、さらに
# `ON CONFLICT` は NULL 同士を別物と見るため**衝突せず 3 行目が増える**。
# つまり nullable のまま PK に足すと、後勝ちを直す代わりに「同じキーの行が
# 無限に増える」別の静かな事故に化ける（同じ罠は §3.3 の `ir_disclosures`
# `doc_id IS NULL` でも既に指摘されている）。
# よって `cloud_store/financials.py` が連結区分を判定できないレコードへ
# `UNKNOWN_CONSOLIDATED`（'不明'）を明示的に入れる。値の推測ではなく
# 「判定できなかった」を名前で持つだけなので §3-1 に反しない。
#
# ## `accounting_standard` は PK に入れない
#
# 会計基準は (銘柄, 期末, 連結区分) に対して通常 1 値で、連結/単体のように
# 同一開示へ同時に載るものではない。一方で PK に入れると、項目の一部しか
# 載せない訂正開示で基準が導出できなかったとき（'不明'）**完全な行と別行に
# 割れて**訂正が本体へ届かなくなる。日本基準 → IFRS の移行期に同一期末が
# 2 基準で開示されると後勝ちが残るが、その場合は `disclosed_at` ガードにより
# 新しい開示が勝つので「最新の基準に寄る」という妥当な側へ倒れる。
_FINANCIALS = """
CREATE TABLE IF NOT EXISTS jss_financials (
  code                      TEXT NOT NULL,
  fiscal_period_end         TEXT NOT NULL,
  disclosure_type           TEXT NOT NULL,
  stock_id                  INTEGER,
  consolidated              TEXT NOT NULL,
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
  PRIMARY KEY (code, fiscal_period_end, disclosure_type, consolidated)
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

# Notion 行 ID の写し（L-20）。tdnet_hourly が毎時 ① 全 3,846 行をスキャン
# （39 req × 14 run/日）していたのを、D1 の 1 SELECT に置き換える。
# 書くのは月次の master_sync だけ（writer claim は jss_* 一律 all/stockStock）。
# `db` は写しの区画: "stock_master"（code→page_id）と
# "stock_master_by_edinet"（EDINETコード→銘柄コード。page_id 列にコードを
# 入れる。大量保有報告書の対象会社解決用）。4 列に収めるため列を足さない。
_NOTION_PAGES = """
CREATE TABLE IF NOT EXISTS jss_notion_pages (
  code       TEXT NOT NULL,
  db         TEXT NOT NULL,
  page_id    TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (db, code)
)
"""

# ③財務サマリの PK。`cloud_store/financials.py` の ON CONFLICT はこれを読む。
#
# `_FINANCIALS` の DDL 側は同じ内容を**リテラルで別に持つ**（導出にしない）。
# 片方を導出にすると、定数から 1 要素を削る変異が DDL と ON CONFLICT の両方へ
# 同時に効いてしまい、テストが追随して検出できなくなる
# （`cloud_store/core_stocks.py` の `BASE_COLUMNS` と同じ理由）。
# 2 本が食い違ったら `tests/test_cloud_schema.py` の突き合わせが落ちる。
FINANCIALS_PK: tuple[str, ...] = (
    "code",
    "fiscal_period_end",
    "disclosure_type",
    "consolidated",
)

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
    _NOTION_PAGES,
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
# | `sector33` | stockStock `master_sync`（東証33業種の名称へ正規化して充填） | EDINET コードリストの「提出者業種」 | **commercial-ok** |
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
# `sector33` は 2026-09-13 まで本番 3,818 行すべて NULL で、同日から
# `master_sync` が埋める（`cloud_store/core_stocks.build_sector33_updates`）。
# EDINET の業種は提出者の申告なので `sector` と約 4% の銘柄で分類が違うが、
# `sector` へ寄せてはいけない（寄せた値は JPX 由来 = personal-only になる）。
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
        # 一次開示で判明したライフサイクル。設計書 §A-1 の追加列表が
        # commercial-ok と決めている（EDINET・TDnet の開示が一次ソース）。
        "listing_status": LicenseTag.COMMERCIAL_OK,
        "listing_date": LicenseTag.COMMERCIAL_OK,
        "delisting_date": LicenseTag.COMMERCIAL_OK,
        # 来歴メタ。第三者由来の値を 1 バイトも含まない（どのソースから・いつ
        # 取ったか、という stockStock 自身の記録）。設計書 §A-1 は「メタ」と
        # 書いているが、タグは 3 値しかないので最も緩い側で明示する。
        # **タグは「値が第三者の著作物・データセットを含むか」を答える列**で、
        # 「公開 API が出すべきか」ではない（後者は API 設計の判断）。
        "license_tag": LicenseTag.COMMERCIAL_OK,
        "src_source": LicenseTag.COMMERCIAL_OK,
        "src_data_date": LicenseTag.COMMERCIAL_OK,
        "src_fetched_at": LicenseTag.COMMERCIAL_OK,
        "quality": LicenseTag.COMMERCIAL_OK,
        # 宣言しない 5 列: `id` / `is_active` / `is_yutai` / `created_at` /
        # `updated_at`。いずれも kabulab-cf が書く既存列で、タグを決めるには
        # 派生元の判断が要る（`is_active` は JPX data_j に載っているかで決まる
        # ので継承すれば personal-only、`is_yutai` は yutai_benefits（みんかぶ）
        # 由来。ただしどちらも「上場している」「優待がある」という公開の事実
        # でもあり、EDINET 上場区分から独立に作れる）。**推測で埋めない**
        # （§3-1）。未宣言の列は「公開してよいと決まっていない」= 公開投影に
        # 入らないので、漏れる方向には倒れない。`jobs/license_map.py` が
        # 毎日この 5 列を名前つきで報告する。
    },
}


def column_license_rows() -> list[list[str]]:
    """jss_column_license へ投入する行を licensing の定義から組み立てる。"""
    rows: list[list[str]] = []
    for table, columns in sorted(MIXED_LICENSE_COLUMNS.items()):
        for column, tag in sorted(columns.items()):
            rows.append([table, column, tag.value])
    return rows


def index_symbol_rows() -> list[list[str]]:
    """jss_index_symbols へ投入する行。Yahoo シンボル未確認のものは含めない。"""
    return [
        [slug, sym, name, f"index/{slug}.json", LicenseTag.PERSONAL_ONLY.value]
        for slug, sym, name in INDEX_SYMBOLS
        if sym
    ]


@dataclass(frozen=True)
class ReferenceDiff:
    """宣言（コード）と実表（D1）の差分。I/O を持たない純粋な比較結果。

    - `missing`: 宣言にあって実表に無い行。投入が届いていない
    - `mismatched`: キーは一致するが値が違う行。`(キー, 実表の値, 宣言の値)`
    - `orphan`: 実表にあって宣言に無い行。**upsert では消えない**ので溜まる
    """

    missing: tuple[tuple[str, ...], ...] = ()
    mismatched: tuple[tuple[tuple[str, ...], str, str], ...] = ()
    orphan: tuple[tuple[str, ...], ...] = ()


def _diff(declared: dict[tuple[str, ...], str], observed: dict[tuple[str, ...], str]) -> ReferenceDiff:
    return ReferenceDiff(
        missing=tuple(sorted(k for k in declared if k not in observed)),
        mismatched=tuple(
            (k, observed[k], declared[k])
            for k in sorted(declared)
            if k in observed and observed[k] != declared[k]
        ),
        orphan=tuple(sorted(k for k in observed if k not in declared)),
    )


COLUMN_LICENSE_SQL = "SELECT table_name, column_name, license_tag FROM jss_column_license"
INDEX_SYMBOLS_SQL = "SELECT slug, yahoo_symbol FROM jss_index_symbols"

# 孤児宣言の削除。キーを明示して 1 行ずつ消す。
# `NOT IN (...)` の 1 文にまとめない理由: D1 のバインドパラメータ上限は 100 で、
# 宣言が増えると静かに上限へ当たる（`d1.MAX_BOUND_PARAMS`）。孤児は稀なので
# 往復回数より上限の安全側を採る。
COLUMN_LICENSE_DELETE_SQL = (
    "DELETE FROM jss_column_license WHERE table_name = ? AND column_name = ?"
)


def column_license_diff(observed_rows: Iterable[dict]) -> ReferenceDiff:
    """`jss_column_license` の実表と宣言を突き合わせる。

    **upsert は `conflict=(table_name, column_name)` なので DELETE を伴わない。**
    宣言から外した列の行はそのまま残り続ける。残った行が personal-only なら
    害は無いが、**commercial-ok の孤児が残ると「公開してよい」と宣言したまま
    誰も管理していない列**ができる。地図が実体と一致していることを別途
    確かめる必要がある。
    """
    declared = {(t, c): tag for t, c, tag in column_license_rows()}
    observed = {
        (str(r.get("table_name") or ""), str(r.get("column_name") or "")): str(
            r.get("license_tag") or ""
        )
        for r in observed_rows
    }
    return _diff(declared, observed)


def index_symbol_diff(observed_rows: Iterable[dict]) -> ReferenceDiff:
    """`jss_index_symbols` の実表と宣言を突き合わせる（slug → yahoo_symbol）。

    `nkvi` は Yahoo シンボルが未確認で投入しないので、**実表に無いのが正常**。
    `index_symbol_rows()` に入っていない slug は宣言に無い扱いになるため、
    未確認のものが `missing` に出ることはない。
    """
    declared = {(row[0],): str(row[1]) for row in index_symbol_rows()}
    observed = {
        (str(r.get("slug") or ""),): str(r.get("yahoo_symbol") or "") for r in observed_rows
    }
    return _diff(declared, observed)


def apply_schema(store) -> int:
    """jss_* を冪等に作成する。作成した文の数を返す。

    D1 は SQLite なので IF NOT EXISTS が効き、既存データは壊さない。

    **既存テーブルの PK は変えられない。** `CREATE TABLE IF NOT EXISTS` は表が
    あれば黙って no-op になるので、この関数を流しても本番 `jss_financials` の
    PK は旧定義（`consolidated` 抜き）のままである。移行は 0 行のうちに
    `DROP TABLE` + `CREATE TABLE` + 索引再作成を人が打つ必要がある（手順は PR 本文）。

    移行前でも壊れた行が入ることは無い: `cloud_store/financials.py` の
    `ON CONFLICT (code, fiscal_period_end, disclosure_type, consolidated)` は
    一致する UNIQUE 制約が無いと D1 がエラーを返すため、writer は**黙って
    後勝ちする代わりに失敗する**（失敗は cloud_failed に計上され収集は止まらない）。
    """
    for statement in SCHEMA_STATEMENTS:
        store.query(statement.strip())
    logger.info("D1 スキーマ適用: %d 文", len(SCHEMA_STATEMENTS))
    return len(SCHEMA_STATEMENTS)


def seed_index_symbols(store) -> int:
    """`jss_index_symbols` へ宣言を投入する。投入した行数を返す。

    日次ジョブは差分があるときだけ呼ぶ（L-17）。宣言と実表が一致して
    いれば upsert を打たない。ブートストラップ（一括投入）は
    `seed_reference_tables` を手で 1 回流す。
    """
    symbols = index_symbol_rows()
    store.upsert(
        "jss_index_symbols",
        ["slug", "yahoo_symbol", "name_ja", "r2_key", "license_tag"],
        symbols,
        conflict=["slug"],
    )
    logger.info(
        "指数シンボルを投入: %d 件（未確認 %d 件はスキップ）",
        len(symbols), len(INDEX_SYMBOLS) - len(symbols),
    )
    return len(symbols)


def seed_column_license(store) -> int:
    """`jss_column_license` へ宣言を投入する。投入した行数を返す。

    `seed_index_symbols` と同じく、日次は差分があるときだけ呼ぶ。
    """
    rows = column_license_rows()
    store.upsert(
        "jss_column_license",
        ["table_name", "column_name", "license_tag"],
        rows,
        conflict=["table_name", "column_name"],
    )
    logger.info("列ライセンス地図を投入: %d 件", len(rows))
    return len(rows)


def seed_reference_tables(store) -> None:
    """参照表（指数シンボル・列ライセンス）を一括投入する。

    **呼び出し元は `jobs/license_map.py` と手動ブートストラップだけ**。
    ここを直接呼ぶ新しい経路を増やさないこと（宣言の投入口が複数あると、
    どれが最後に走ったかで実表の内容が変わる）。日次ジョブは
    `seed_index_symbols` / `seed_column_license` を差分があるときだけ呼ぶ。
    """
    seed_index_symbols(store)
    seed_column_license(store)


# `jss_*` が本番に存在するかを sqlite_master で確かめる 1 文。
# **行を走査しない**（sqlite_master はスキーマのカタログ）。
JSS_TABLES_SQL = (
    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'jss_%' ORDER BY name"
)


def declared_tables() -> frozenset[str]:
    """`SCHEMA_STATEMENTS` の CREATE TABLE から表名を取り出す。"""
    names = set()
    for statement in SCHEMA_STATEMENTS:
        head = statement.strip()
        if head.upper().startswith("CREATE TABLE"):
            names.add(head.split("(", 1)[0].split()[-1])
    return frozenset(names)


__all__ = [
    "COLUMN_LICENSE_DELETE_SQL",
    "COLUMN_LICENSE_SQL",
    "FINANCIALS_PK",
    "INDEX_SYMBOLS",
    "INDEX_SYMBOLS_SQL",
    "JSS_TABLES_SQL",
    "MIXED_LICENSE_COLUMNS",
    "SCHEMA_STATEMENTS",
    "ReferenceDiff",
    "apply_schema",
    "column_license_diff",
    "column_license_rows",
    "declared_tables",
    "index_symbol_diff",
    "index_symbol_rows",
    "seed_column_license",
    "seed_index_symbols",
    "seed_reference_tables",
]
