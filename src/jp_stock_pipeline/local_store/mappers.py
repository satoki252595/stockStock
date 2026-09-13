"""正規化レコード → (SQL, params) の純粋関数群。

副作用を持たず DB 接続も要らないため、SQL 文・パラメータの正しさを単体テスト
できる。Notion 側 (notion/upsert.py) と挙動を対称に保つ（③ を除く。下記）:
- 完全置換: ON CONFLICT DO UPDATE で非PK列を EXCLUDED 上書き（None も含め前回値を
  残さない §3-1）。①②④⑤ は 1 回の書き込みがその時点の行の全項目を運ぶデータ
  なので、None は「その値が無い」を意味し、前回値を残す方が嘘になる。
- ① のライフサイクル列 (status/上場日/上場廃止日) は二重所有を避けるため
  include_lifecycle で出し分け、確定は一次開示の lifecycle_update が担う (§ Phase3)。

## ③ 財務サマリだけが完全置換の例外である理由

③ は**一部の項目しか運ばない書き込み**が同じ PK に着地するデータである。
`jobs/edinet_daily.py` は doc_type_code 120（有報）と 130（訂正有報）の両方を
`disclosure_type="本決算"` に落とし、訂正報告書は訂正した項目だけを載せる。
`transform/normalize.tidy_to_financial_record` は決算期末さえ導出できれば残りが
None でもレコードを返す。ここでの None は「値が無い」ではなく「今回は運んでいない」
なので完全置換の前提が成り立たず、完全置換のままだと「売上だけ訂正した開示」が
EPS・CF・配当を全部 NULL にする。

そこで financial_upsert は D1 側 (`cloud_store/financials.py`) と同じ意味論にする:
- 値の列は `COALESCE(EXCLUDED.c, financials.c)` でマージする（今回運ばれなかった
  項目は前回値を残す。値の捏造ではない）。「訂正が値を取り下げた」と「今回は運んで
  いない」は XBRL から区別できないので、既知の値を残す側へ倒す。
- `license_tag` は厳しい側を残す（EDINET 行へ TDnet の訂正が入る／その逆で、
  値は混在するのにタグだけ緩い側へ洗われるのを防ぐ）。
- `source` / `fetched_at` / `quality` は NOT NULL なので上書きする（行が表せる
  来歴は最後の書き手だけ）。
- `disclosed_at` ガードは維持する（古い開示の再取得が新しい訂正を巻き戻さない）。

**PK に連結区分を含める**（D1 は PR #39。既存のローカル DB は `local_store/schema.py`
の FINANCIALS_PK_MIGRATION が接続時に PK を張り替える）。連結と単体が別行になるので、
COALESCE のマージは必ず同じ測定範囲の中で起き、「単体の訂正値 + 前回の連結値」が
1 行に混ざらない。PK に無かった頃は、連結区分が一致するときだけマージする
`merge_scope` で防いでいたが、PK に入ったので ③ では使わなくなった。
判定できなかったレコードの連結区分は D1 と同じ '不明' (UNKNOWN_CONSOLIDATED) にする。
PostgreSQL の PK 列は NULL を許さないため。

連結区分が '不明' の既存行を、後から来た連結/単体のレコードが採用することはしない
（D1 と同じく別行にする）。Notion ③ は採用する (notion/upsert.pick_financial_page) が、
ローカルは本番で未構成で、Notion ③ 34,551 行に連結単体が空の行は 0 件だった
(2026-09-13 監査)。採用のために ON CONFLICT の前に条件付き UPDATE を足すと、
開示日時ガードと完全置換の順序まで SQL で揃える必要があり、利益に見合わない。

Notion ③ (notion/upsert.py) は完全置換のままで、③ に限りここと対称ではない。

値はプレースホルダ (%(name)s) で渡し、文字列連結しない（SQL インジェクション対策）。
SQL へ埋め込むリテラルはライセンスタグ enum の定数だけ（licensing.stricter_tag_sql）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..licensing import stricter_tag_sql

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..models import (
        DisclosureRecord,
        FinancialSummaryRecord,
        PriceTechnicalRecord,
        Provenance,
        RawArtifact,
        StockMasterRecord,
    )

# ① 状態 select 値 / ライフサイクル書類種別 (notion/upsert.py と一致させる)
STATUS_LISTED = "上場"
STATUS_DELISTED = "上場廃止"
DOC_TYPE_DELISTING = "上場廃止"
DOC_TYPE_NEW_LISTING = "新規上場"
LIFECYCLE_DOC_TYPES = frozenset({DOC_TYPE_DELISTING, DOC_TYPE_NEW_LISTING})


def _prov(prov: Provenance) -> dict:
    """共通の来歴列 (§6.3)。data_date を持つテーブル向け。"""
    return {
        "source": prov.source.value,
        "license_tag": prov.license_tag.value,
        "data_date": prov.data_date,
        "fetched_at": prov.fetched_at,
        "quality": prov.quality.value,
    }


def _build_upsert(
    table: str,
    params: dict,
    pk: list[str],
    *,
    exclude_cols: tuple[str, ...] = (),
    guard_column: str | None = None,
    merge_cols: tuple[str, ...] = (),
    merge_scope: str | None = None,
    license_column: str | None = None,
) -> tuple[str, dict]:
    """INSERT ... ON CONFLICT (pk) DO UPDATE を生成する。

    params の挿入順がそのまま列順になる（dict は挿入順を保持）。
    exclude_cols は params から除外（INSERT 列にも UPDATE にも含めない）。

    既定は完全置換（非PK列を `c = EXCLUDED.c`）。以下の 3 つは ③ のように一部の
    項目しか運ばない書き込みのための例外で、指定した列だけが変わる（冒頭 docstring）:
    - merge_cols: `c = COALESCE(EXCLUDED.c, <table>.c)`。今回 None の列は前回値を残す。
    - merge_scope: merge_cols の COALESCE を `EXCLUDED.<s> IS NOT DISTINCT FROM
      <table>.<s>` のときに限り、一致しなければその列も完全置換にする
      （PK に無い測定範囲の違う値を 1 行に混ぜない）。
    - license_column: 厳しい側のタグを残す（licensing.stricter_tag_sql）。
    どれも params の非PK列でなければ ValueError（タイポを黙って完全置換にしない）。

    guard_column を指定すると DO UPDATE に WHERE 句を付け、
    `EXCLUDED.<col> >= <table>.<col> OR <table>.<col> IS NULL` を満たすときだけ
    上書きする（#14: 開示日時等で「古い方が新しい方を巻き戻す」のを防ぐ）。
    条件を満たさない場合は Postgres の仕様どおり INSERT 自体が何もしない
    （エラーにはならず、既存行がそのまま残る）。EXCLUDED 側が NULL（今回の
    値が不明）なら比較は NULL＝偽になり上書きしない。既存側が NULL
    （過去に未設定）なら常に許可する（守るべき既知の値が無いため）。
    """
    columns = [c for c in params if c not in exclude_cols]
    send = {c: params[c] for c in columns}
    col_list = ", ".join(columns)
    placeholders = ", ".join(f"%({c})s" for c in columns)
    update_cols = [c for c in columns if c not in pk]
    for name in (*merge_cols, merge_scope, license_column):
        if name is not None and name not in update_cols:
            raise ValueError(f"{name!r} が params の非PK列に無い: {table}")
    if merge_scope in merge_cols or license_column in merge_cols:
        raise ValueError(f"merge_scope / license_column を merge_cols と重ねられない: {table}")
    assignments = []
    for c in update_cols:
        if c == license_column:
            value = stricter_tag_sql(f"EXCLUDED.{c}", f"{table}.{c}")
        elif c in merge_cols:
            value = f"COALESCE(EXCLUDED.{c}, {table}.{c})"
            if merge_scope is not None:
                value = (
                    f"CASE WHEN EXCLUDED.{merge_scope} IS NOT DISTINCT FROM"
                    f" {table}.{merge_scope} THEN {value} ELSE EXCLUDED.{c} END"
                )
        else:
            value = f"EXCLUDED.{c}"
        assignments.append(f"{c} = {value}")
    set_clause = ", ".join([*assignments, "updated_at = now()"])
    pk_clause = ", ".join(pk)
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({pk_clause}) DO UPDATE SET {set_clause}"
    )
    if guard_column:
        if guard_column not in columns:
            raise ValueError(f"guard_column {guard_column!r} が params に無い: {table}")
        sql += (
            f" WHERE EXCLUDED.{guard_column} >= {table}.{guard_column}"
            f" OR {table}.{guard_column} IS NULL"
        )
    return sql, send


def stock_master_upsert(
    record: StockMasterRecord, *, include_lifecycle: bool = True
) -> tuple[str, dict]:
    """① 銘柄マスタ。include_lifecycle=False では status/上場日/上場廃止日を
    一切触らない（INSERT 列にも含めない＝新規は NULL、既存は保持。月次 codelist
    同期が開示由来の状態を上書きしないため § Phase3）。"""
    params: dict = {
        "code": record.code,
        "name": record.name,
        "market": record.market,
        "sector33": record.sector33,
        "sector17": record.sector17,
        "edinet_code": record.edinet_code,
        "listed": record.listed,
        **_prov(record.provenance),
    }
    if include_lifecycle:
        params["status"] = record.status
        params["listing_date"] = record.listing_date
        params["delisting_date"] = record.delisting_date
    return _build_upsert("stock_master", params, ["code"])


def price_upsert(record: PriceTechnicalRecord) -> tuple[str, dict]:
    """② 株価テクニカル。(code, data_date) を主キーに時系列で蓄積する。

    data_date が None のレコードは主キーを構成できないため呼び出し側が除外する。
    """
    prov = record.provenance
    params = {
        "code": record.code,
        "data_date": prov.data_date,
        "open": record.open,
        "high": record.high,
        "low": record.low,
        "close": record.close,
        "prev_close_pct": record.prev_close_pct,
        "volume": record.volume,
        "turnover": record.turnover,
        "market_cap": record.market_cap,
        "week52_high": record.week52_high,
        "week52_low": record.week52_low,
        "sma5": record.sma5,
        "sma25": record.sma25,
        "sma75": record.sma75,
        "sma200": record.sma200,
        "sma25_dev_pct": record.sma25_dev_pct,
        "rsi14": record.rsi14,
        "macd": record.macd,
        "macd_signal": record.macd_signal,
        "macd_hist": record.macd_hist,
        "bb_upper": record.bb_upper,
        "bb_lower": record.bb_lower,
        "atr14": record.atr14,
        "volume_ratio25": record.volume_ratio25,
        "per": record.per,
        "pbr": record.pbr,
        "dividend_yield_pct": record.dividend_yield_pct,
        "source": prov.source.value,
        "license_tag": prov.license_tag.value,
        "fetched_at": prov.fetched_at,
        "quality": prov.quality.value,
    }
    return _build_upsert("prices", params, ["code", "data_date"])


# ③ の列の役割（冒頭 docstring「③ 財務サマリだけが完全置換の例外である理由」）。
FIN_PK: tuple[str, ...] = ("code", "fiscal_period_end", "disclosure_type", "consolidated")
# NOT NULL の来歴列。COALESCE しても必ず EXCLUDED 側が残るので明示的に上書きする。
FIN_OVERWRITE_COLUMNS: tuple[str, ...] = ("source", "fetched_at", "quality")
FIN_LICENSE_COLUMN = "license_tag"
# 連結区分を判定できなかったレコードの PK 値。D1 (cloud_store/financials.py) と同じ値。
UNKNOWN_CONSOLIDATED = "不明"


def financial_upsert(record: FinancialSummaryRecord) -> tuple[str, dict]:
    """③ 財務サマリ。(code, 決算期末, 開示種別, 連結区分) を主キー。

    完全置換ではなくマージする（冒頭 docstring）。上の分類に入らない列はすべて
    COALESCE マージになるので、列を足しても訂正開示の NULL 潰しは再発しない
    （完全置換を既定にすると、足した列だけが黙って潰れる）。
    """
    params = {
        "code": record.code,
        "fiscal_period_end": record.fiscal_period_end,
        "disclosure_type": record.disclosure_type,
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
        "disclosed_at": record.disclosed_at,
        **_prov(record.provenance),
    }
    fixed = {*FIN_PK, *FIN_OVERWRITE_COLUMNS, FIN_LICENSE_COLUMN}
    return _build_upsert(
        "financials", params, list(FIN_PK),
        guard_column="disclosed_at",
        merge_cols=tuple(c for c in params if c not in fixed),
        license_column=FIN_LICENSE_COLUMN,
    )


def disclosure_upsert(record: DisclosureRecord) -> tuple[str, dict]:
    """④ 開示書類。doc_id を主キー。"""
    params = {
        "doc_id": record.doc_id,
        "title": record.title,
        "disclosed_at": record.disclosed_at,
        "code": record.code,
        "doc_type": record.doc_type,
        "source_url": record.source_url,
        "has_xbrl": record.has_xbrl,
        "split_ratio": record.split_ratio,
        "split_factor": record.split_factor,
        "effective_date": record.effective_date,
        **_prov(record.provenance),
    }
    return _build_upsert("disclosures", params, ["doc_id"])


def raw_file_upsert(artifact: RawArtifact) -> tuple[str, dict]:
    """⑤ 原本ファイル（メタのみ）。sha256 を主キー。"""
    params = {
        "sha256": artifact.sha256,
        "filename": artifact.filename,
        "source": artifact.source.value,
        "datatype": artifact.datatype,
        "scope": artifact.scope,
        "data_date": artifact.data_date,
        "fetched_at": artifact.fetched_at,
        "url": artifact.url,
        "size_bytes": artifact.size_bytes,
        "license_tag": artifact.license_tag.value,
        "convert_status": artifact.convert_status.value,
        "notion_page_id": artifact.notion_page_id,
    }
    return _build_upsert("raw_files", params, ["sha256"])


_XBRL_FACT_COLUMNS: tuple[str, ...] = (
    "doc_id", "element", "context_ref", "code", "period_start", "period_end",
    "instant_date", "consolidated", "unit", "value", "is_text_block",
    "source", "license_tag", "fetched_at",
)
_XBRL_FACT_PK: tuple[str, ...] = ("doc_id", "element", "context_ref")


def xbrl_facts_insert(
    rows: "Iterable[dict]", artifact: RawArtifact
) -> tuple[str, list[dict]]:
    """⑧ XBRL 全ファクト（ローカル専用）。doc 単位の tidy 行を bulk upsert する。

    - rows は convert.xbrl_to_csv の tidy レコード（dict）。value が空（nil/欠損 §3-1）
      の行は格納しない（欠損は格納せず＝非存在で表現）。
    - PK=(doc_id, element, context_ref)。同一バッチ内の PK 重複は最後の値で de-dup する
      （決定的な last-wins と冗長 upsert の削減。複数 .xbrl を同一 doc_id でパースする
      EDINET 等で同一 PK が異なる値で現れた場合は後勝ちになる点に注意）。
    - 来歴 source/license_tag/fetched_at は原本 artifact から付与（行ごとにライセンスを
      持たせ、公開面で commercial-ok のみフィルタできるようにする §2.2）。
    - 返り値: (executemany 用 SQL, パラメータ dict のリスト)。リストが空なら呼び出し側は
      実行しない。
    """
    source = artifact.source.value
    license_tag = artifact.license_tag.value
    fetched_at = artifact.fetched_at
    deduped: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        raw_value = row.get("value")
        value = raw_value.strip() if isinstance(raw_value, str) else raw_value
        if not value:
            continue  # nil/欠損は格納しない（§3-1）
        doc_id = row.get("doc_id") or ""
        element = row.get("element") or ""
        context_ref = row.get("context_ref") or ""
        if not (doc_id and element and context_ref):
            continue  # PK を構成できない行はスキップ
        deduped[(doc_id, element, context_ref)] = {
            "doc_id": doc_id,
            "element": element,
            "context_ref": context_ref,
            "code": row.get("code") or None,
            "period_start": row.get("period_start") or None,
            "period_end": row.get("period_end") or None,
            "instant_date": row.get("instant_date") or None,
            "consolidated": row.get("consolidated") or None,
            "unit": row.get("unit") or None,
            "value": value,
            "is_text_block": element.endswith("TextBlock"),
            "source": source,
            "license_tag": license_tag,
            "fetched_at": fetched_at,
        }
    col_list = ", ".join(_XBRL_FACT_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in _XBRL_FACT_COLUMNS)
    update_cols = [c for c in _XBRL_FACT_COLUMNS if c not in _XBRL_FACT_PK]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    pk_clause = ", ".join(_XBRL_FACT_PK)
    sql = (
        f"INSERT INTO xbrl_facts ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({pk_clause}) DO UPDATE SET {set_clause}, updated_at = now()"
    )
    return sql, list(deduped.values())


def job_log_insert(
    job_name: str,
    status: str,
    processed: int,
    failed: int,
    failed_codes: list[str],
    run_url: str | None,
    duration_secs: float | None,
) -> tuple[str, dict]:
    """⑦ 収集ジョブログ。INSERT のみ（1行=1実行の履歴を蓄積）。"""
    params = {
        "job_name": job_name,
        "status": status,
        "processed": processed,
        "failed": failed,
        "failed_codes": ",".join(failed_codes) if failed_codes else None,
        "run_url": run_url,
        "duration_secs": duration_secs,
    }
    columns = list(params)
    col_list = ", ".join(columns)
    placeholders = ", ".join(f"%({c})s" for c in columns)
    return f"INSERT INTO job_log ({col_list}) VALUES ({placeholders})", params


def mark_absent_update(code: str) -> tuple[str, dict]:
    """コードリスト消失 → listed=False のみ（状態は一次開示が所有 § Phase3）。"""
    return (
        "UPDATE stock_master SET listed = FALSE, updated_at = now() WHERE code = %(code)s",
        {"code": code},
    )


def lifecycle_update(record: DisclosureRecord) -> tuple[str, dict] | None:
    """上場廃止/新規上場の一次開示 → ① の状態を部分更新する (§ Phase3)。

    状態(select)は確定値として常に設定。日付は effective_date が取れた時のみ書き、
    取れないときは触らない（既存の確定日付を消さない / 発表日を流用しない §3-1）。
    対象外の書類種別なら None。
    """
    if record.doc_type not in LIFECYCLE_DOC_TYPES or not record.code:
        return None
    if record.doc_type == DOC_TYPE_DELISTING:
        status, date_col = STATUS_DELISTED, "delisting_date"
    else:
        status, date_col = STATUS_LISTED, "listing_date"
    sets = ["status = %(status)s"]
    params: dict = {"status": status, "code": record.code}
    if record.effective_date is not None:
        sets.append(f"{date_col} = %(eff_date)s")
        params["eff_date"] = record.effective_date
    sets.append("updated_at = now()")
    sql = f"UPDATE stock_master SET {', '.join(sets)} WHERE code = %(code)s"
    return sql, params
