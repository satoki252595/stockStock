"""ローカル PostgreSQL スキーマ DDL (Notion ①〜⑤⑦ に対応)。

設計方針:
- Notion の正規化オブジェクト(models.py の dataclass)を素直に列へ展開する。
- 共通の来歴 (§6.3): source / license_tag / data_date / fetched_at / quality を各行に持つ。
- ②株価は Notion が「最新スナップショット」なのに対し、ローカルは
  (code, data_date) を主キーに**時系列を蓄積**する(API の価値が上がる §7)。
- 数値はすべて NULL 許容（取得できなかった値は NULL のまま。捏造しない §3-1）。
- DDL は冪等 (IF NOT EXISTS)。LocalStore.init_schema() が起動時に流す。
"""

from __future__ import annotations

# ③ financials の PK に連結区分を足す移行（既存 DB 向け。新規 DB は下の DDL が
# 最初から 4 列の PK で作るので何もしない）。
#
# - 旧 PK (code, 決算期末, 開示種別) の下では各キーが 1 行なので、4 列の PK に
#   張り替えても衝突しない。既存行はそのまま残る（消さない・重複させない）。
# - PostgreSQL の PK 列は NULL を許さない。連結区分が NULL の既存行は、D1 と同じ
#   '不明' (local_store/mappers.UNKNOWN_CONSOLIDATED) を入れてから張り替える。
# - 冪等: PK が既に連結区分を含んでいれば、ロックも UPDATE も行わず終わる。
#   LocalStore.connect のたびに流れるので、確認は pg_constraint を 1 回引くだけにする。
# - 同時に 2 つの接続が移行を始めても壊れないよう、ロックを取ってから確認し直す。
#   READ COMMITTED では文ごとにスナップショットを取り直すので、ロック待ちの間に
#   相手が済ませた張り替えが見える。
# - 1 つの DO ブロックは 1 トランザクションなので、途中で失敗すれば元の PK に戻る。
# - `format('%I')` を使わず quote_ident で連結する。psycopg に渡す SQL に % を
#   含めないため。
FINANCIALS_PK_MIGRATION = """
DO $$
DECLARE
    old_pk text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
         WHERE c.conrelid = 'financials'::regclass AND c.contype = 'p'
           AND NOT EXISTS (
               SELECT 1 FROM pg_attribute a
                WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                  AND a.attname = 'consolidated'
           )
    ) THEN
        RETURN;
    END IF;
    LOCK TABLE financials IN ACCESS EXCLUSIVE MODE;
    SELECT c.conname INTO old_pk FROM pg_constraint c
     WHERE c.conrelid = 'financials'::regclass AND c.contype = 'p'
       AND NOT EXISTS (
           SELECT 1 FROM pg_attribute a
            WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
              AND a.attname = 'consolidated'
       );
    IF old_pk IS NULL THEN
        RETURN;
    END IF;
    UPDATE financials SET consolidated = '不明' WHERE consolidated IS NULL;
    ALTER TABLE financials ALTER COLUMN consolidated SET NOT NULL;
    EXECUTE 'ALTER TABLE financials DROP CONSTRAINT ' || quote_ident(old_pk);
    ALTER TABLE financials
        ADD PRIMARY KEY (code, fiscal_period_end, disclosure_type, consolidated);
END
$$
"""

# 各文を個別に実行する（psycopg は複数文 execute も可だが、冪等性と可読性のため分割）。
SCHEMA_STATEMENTS: tuple[str, ...] = (
    # ① 銘柄マスタ -----------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS stock_master (
        code            TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        market          TEXT,
        sector33        TEXT,
        sector17        TEXT,
        edinet_code     TEXT,
        listed          BOOLEAN NOT NULL DEFAULT TRUE,
        status          TEXT,
        listing_date    DATE,
        delisting_date  DATE,
        source          TEXT NOT NULL,
        license_tag     TEXT NOT NULL,
        data_date       DATE,
        fetched_at      TIMESTAMPTZ NOT NULL,
        quality         TEXT NOT NULL,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ② 株価テクニカル（時系列: code × data_date） ---------------------------
    """
    CREATE TABLE IF NOT EXISTS prices (
        code               TEXT NOT NULL,
        data_date          DATE NOT NULL,
        open               DOUBLE PRECISION,
        high               DOUBLE PRECISION,
        low                DOUBLE PRECISION,
        close              DOUBLE PRECISION,
        prev_close_pct     DOUBLE PRECISION,
        volume             DOUBLE PRECISION,
        turnover           DOUBLE PRECISION,
        market_cap         DOUBLE PRECISION,
        week52_high        DOUBLE PRECISION,
        week52_low         DOUBLE PRECISION,
        sma5               DOUBLE PRECISION,
        sma25              DOUBLE PRECISION,
        sma75              DOUBLE PRECISION,
        sma200             DOUBLE PRECISION,
        sma25_dev_pct      DOUBLE PRECISION,
        rsi14              DOUBLE PRECISION,
        macd               DOUBLE PRECISION,
        macd_signal        DOUBLE PRECISION,
        macd_hist          DOUBLE PRECISION,
        bb_upper           DOUBLE PRECISION,
        bb_lower           DOUBLE PRECISION,
        atr14              DOUBLE PRECISION,
        volume_ratio25     DOUBLE PRECISION,
        per                DOUBLE PRECISION,
        pbr                DOUBLE PRECISION,
        dividend_yield_pct DOUBLE PRECISION,
        source             TEXT NOT NULL,
        license_tag        TEXT NOT NULL,
        fetched_at         TIMESTAMPTZ NOT NULL,
        quality            TEXT NOT NULL,
        updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (code, data_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS prices_code_idx ON prices (code)",
    # ③ 財務サマリ（code × 決算期末 × 開示種別 × 連結区分） -------------------
    """
    CREATE TABLE IF NOT EXISTS financials (
        code                      TEXT NOT NULL,
        fiscal_period_end         DATE NOT NULL,
        disclosure_type           TEXT NOT NULL,
        consolidated              TEXT NOT NULL,
        accounting_standard       TEXT,
        net_sales                 DOUBLE PRECISION,
        operating_income          DOUBLE PRECISION,
        ordinary_income           DOUBLE PRECISION,
        net_income                DOUBLE PRECISION,
        eps                       DOUBLE PRECISION,
        bps                       DOUBLE PRECISION,
        roe_pct                   DOUBLE PRECISION,
        roa_pct                   DOUBLE PRECISION,
        equity_ratio_pct          DOUBLE PRECISION,
        cf_operating              DOUBLE PRECISION,
        cf_investing              DOUBLE PRECISION,
        cf_financing              DOUBLE PRECISION,
        dps_actual                DOUBLE PRECISION,
        dps_forecast              DOUBLE PRECISION,
        forecast_net_sales        DOUBLE PRECISION,
        forecast_operating_income DOUBLE PRECISION,
        forecast_ordinary_income  DOUBLE PRECISION,
        forecast_net_income       DOUBLE PRECISION,
        forecast_eps              DOUBLE PRECISION,
        disclosed_at              TIMESTAMPTZ,
        source                    TEXT NOT NULL,
        license_tag               TEXT NOT NULL,
        data_date                 DATE,
        fetched_at                TIMESTAMPTZ NOT NULL,
        quality                   TEXT NOT NULL,
        updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (code, fiscal_period_end, disclosure_type, consolidated)
    )
    """,
    FINANCIALS_PK_MIGRATION,
    "CREATE INDEX IF NOT EXISTS financials_code_idx ON financials (code)",
    # ④ 開示書類（doc_id 一意） ---------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS disclosures (
        doc_id          TEXT PRIMARY KEY,
        title           TEXT NOT NULL,
        disclosed_at    TIMESTAMPTZ NOT NULL,
        code            TEXT,
        doc_type        TEXT NOT NULL,
        source_url      TEXT,
        has_xbrl        BOOLEAN NOT NULL DEFAULT FALSE,
        split_ratio     TEXT,
        split_factor    DOUBLE PRECISION,
        effective_date  DATE,
        source          TEXT NOT NULL,
        license_tag     TEXT NOT NULL,
        data_date       DATE,
        fetched_at      TIMESTAMPTZ NOT NULL,
        quality         TEXT NOT NULL,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS disclosures_code_idx ON disclosures (code)",
    "CREATE INDEX IF NOT EXISTS disclosures_disclosed_idx ON disclosures (disclosed_at)",
    # ⑤ 原本ファイル（メタのみ。原本バイナリは Notion ⑤ / ローカル data/raw に保管） --
    """
    CREATE TABLE IF NOT EXISTS raw_files (
        sha256          TEXT PRIMARY KEY,
        filename        TEXT NOT NULL,
        source          TEXT NOT NULL,
        datatype        TEXT NOT NULL,
        scope           TEXT NOT NULL,
        data_date       DATE,
        fetched_at      TIMESTAMPTZ NOT NULL,
        url             TEXT NOT NULL,
        size_bytes      BIGINT NOT NULL,
        license_tag     TEXT NOT NULL,
        convert_status  TEXT NOT NULL,
        notion_page_id  TEXT,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ⑧ XBRL 全ファクト（ローカル専用・Notion 非ミラー） ---------------------
    # 有報/短信 XBRL の全ファクト（数値＋定性 textBlock）を保持する。Notion ③ は
    # 厳選した財務サマリのみで、定性情報（事業等のリスク等の TextBlock）や③に
    # 載らない数値は⑤のCSVに埋もれていた。これを横断クエリ＋日本語全文検索できる
    # 形で保持する（§7.1）。来歴: doc_id→④開示 / license_tag を各行に持ち、公開面では
    # commercial-ok のみをフィルタできる（factual-cite=短信原文は内部利用限定 §2.2）。
    # value は変換版そのまま（数値は ix 復号済み文字列・定性は原文テキスト §5.2）。
    """
    CREATE TABLE IF NOT EXISTS xbrl_facts (
        doc_id        TEXT NOT NULL,
        element       TEXT NOT NULL,
        context_ref   TEXT NOT NULL,
        code          TEXT,
        period_start  TEXT,
        period_end    TEXT,
        instant_date  TEXT,
        consolidated  TEXT,
        unit          TEXT,
        value         TEXT NOT NULL,
        is_text_block BOOLEAN NOT NULL DEFAULT FALSE,
        source        TEXT NOT NULL,
        license_tag   TEXT NOT NULL,
        fetched_at    TIMESTAMPTZ NOT NULL,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (doc_id, element, context_ref)
    )
    """,
    "CREATE INDEX IF NOT EXISTS xbrl_facts_code_idx ON xbrl_facts (code)",
    # ⑦ 収集ジョブログ（1行=1ジョブ実行） ----------------------------------
    """
    CREATE TABLE IF NOT EXISTS job_log (
        id              BIGSERIAL PRIMARY KEY,
        job_name        TEXT NOT NULL,
        status          TEXT NOT NULL,
        processed       INTEGER NOT NULL,
        failed          INTEGER NOT NULL,
        failed_codes    TEXT,
        run_url         TEXT,
        duration_secs   DOUBLE PRECISION,
        finished_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS job_log_name_idx ON job_log (job_name, finished_at)",
)

# PGroonga による日本語全文検索インデックス（ベストエフォート）。
# PGroonga 拡張が入っていない端末では CREATE EXTENSION が失敗するため、
# SCHEMA_STATEMENTS とは分離し sink 側で失敗を握って LIKE 検索へフォールバックする。
# 定性 textBlock のみを対象にして索引を小さく保つ。
FTS_STATEMENTS: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS pgroonga",
    "CREATE INDEX IF NOT EXISTS xbrl_facts_value_fts "
    "ON xbrl_facts USING pgroonga (value) WHERE is_text_block",
)
