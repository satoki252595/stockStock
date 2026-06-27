"""reconcile_weekly: 第2ソース（stooq）による ② 終値の突合検証 (§8.2, §3-5)。

J-Quants（旧トラックB）を廃止したため、独立した第2ソースは設計上の指定
フォールバックである **stooq 実データ** を用いる（§3-2, §4。prices_daily でも
yfinance 障害時のフォールバックに採用済み）。

- ② の各行の終値を、stooq の同一データ基準日の終値と突合する
- 乖離閾値（既定 ±1%）超の行に データ品質=要確認 を付与（値は書き換えない §3-5）
- stooq が取得できない / 同一基準日の行が無い銘柄は「突合対象外」として
  正直に記録し、ダミーで埋めない（§3-1。突合しなかったことを隠さない §3-2）
- ③ 財務サマリのバックフィルは EDINET(edinet_daily, commercial-ok) と
  TDnet短信(tdnet_hourly, factual-cite) が担うため、本ジョブでは扱わない

stooq は銘柄ごとに1コール（= 1取得単位 = ⑤の1原本 §5.1）。件数が多い場合は
--limit で段階的に回す（§1, §8.3）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import pandas as pd

from ..collectors import stooq_prices
from ..convert import attach_dataframe_parquet
from ..http import FetchError
from ..licensing import source_license
from ..models import (
    PriceTechnicalRecord,
    Provenance,
    Source,
    now_jst,
)
from ..notion import file_upload
from ..notion import schema as S
from ..notion.client import NotionClient
from ..transform import reconcile
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "reconcile_weekly"


# ---------------------------------------------------------------------------
# ② スナップショット抽出
# ---------------------------------------------------------------------------


def _prop_number(page: dict, name: str) -> float | None:
    return page.get("properties", {}).get(name, {}).get("number")


def _prop_title(page: dict, name: str) -> str:
    items = page.get("properties", {}).get(name, {}).get("title", [])
    return items[0].get("plain_text", "").strip() if items else ""


def _prop_date(page: dict, name: str) -> date | None:
    start = (page.get("properties", {}).get(name, {}).get("date") or {}).get("start")
    if not start:
        return None
    try:
        return datetime.strptime(str(start)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def load_price_snapshot(
    client: NotionClient, settings
) -> list[tuple[str, str, float, date | None]]:
    """② の全行から (page_id, 銘柄コード, 終値, データ基準日) を取り出す。

    終値欠損行は除外（欠損は突合対象外 §3-1）。
    """
    pages = client.query_database(settings.db_id("prices"))
    out: list[tuple[str, str, float, date | None]] = []
    for page in pages:
        code = _prop_title(page, S.PRICE_PROP_CODE)
        close = _prop_number(page, S.PRICE_PROP_CLOSE)
        if code and close is not None:
            out.append(
                (page["id"], code, float(close), _prop_date(page, S.PROP_DATA_DATE))
            )
    return out


# ---------------------------------------------------------------------------
# 突合入力の構築（純粋関数・ネットワーク非依存。テスト可能に切り出す）
# ---------------------------------------------------------------------------


def stooq_close_on(df: pd.DataFrame, data_date: date) -> float | None:
    """stooq 日足から data_date の終値を取り出す。無ければ None（捏造しない §3-1）。"""
    if df.empty or data_date is None:
        return None
    cols = {str(c).lower(): c for c in df.columns}
    date_col, close_col = cols.get("date"), cols.get("close")
    if date_col is None or close_col is None:
        return None
    rows = df[pd.to_datetime(df[date_col]).dt.date == data_date]
    if rows.empty or pd.isna(rows.iloc[-1][close_col]):
        return None
    return float(rows.iloc[-1][close_col])


def build_reconcile_inputs(
    snapshot: list[tuple[str, str, float, date | None]],
    stooq_by_code: dict[str, pd.DataFrame],
) -> tuple[list[PriceTechnicalRecord], pd.DataFrame, list[str]]:
    """② スナップショットと stooq フレーム群から突合入力を組む。

    返り値: (ours_records, theirs_df, skipped_codes)。
    同一データ基準日の stooq 終値が得られた銘柄のみ突合対象にする。
    対象外（stooq 無し / 同一基準日の行が無い）は skipped_codes へ。
    """
    ours: list[PriceTechnicalRecord] = []
    theirs_rows: list[dict] = []
    skipped: list[str] = []
    prov = Provenance(
        source=Source.YFINANCE,  # ② の比較対象キャリア（code/close のみ使用）
        license_tag=source_license(Source.YFINANCE),
        data_date=None,
        fetched_at=now_jst(),
    )
    for _page_id, code, close, data_date in snapshot:
        df = stooq_by_code.get(code)
        theirs = stooq_close_on(df, data_date) if df is not None and data_date else None
        if theirs is None:
            skipped.append(code)
            continue
        ours.append(PriceTechnicalRecord(code=code, close=close, provenance=prov))
        theirs_rows.append({"Code": code, "Close": theirs})
    return ours, pd.DataFrame(theirs_rows), skipped


# ---------------------------------------------------------------------------
# 実行本体
# ---------------------------------------------------------------------------


def execute(ctx: JobContext) -> None:
    snapshot = load_price_snapshot(ctx.client, ctx.settings)
    page_by_code = {code: page_id for page_id, code, _close, _dd in snapshot}
    codes = apply_limit(sorted(page_by_code), ctx.args.limit)
    targets = [row for row in snapshot if row[1] in set(codes)]

    # stooq 実データを銘柄ごとに取得（= 1取得単位。原本は ⑤ へ必須 §8.1-4）
    stooq_by_code: dict[str, pd.DataFrame] = {}
    for code in codes:
        try:
            artifact, df = stooq_prices.fetch_daily(ctx.settings, code)
            attach_dataframe_parquet(artifact, df)  # 型付き変換版 (§5.2)
            file_upload.upload_raw_artifact(ctx.client, ctx.settings, artifact)
            stooq_by_code[code] = df
        except (FetchError, file_upload.RawUploadError) as exc:
            # 取得失敗は欠損として記録（突合できないことを隠さない §3-2）
            logger.info("stooq 突合対象外 %s: %s", code, exc)

    ours, theirs_df, skipped = build_reconcile_inputs(targets, stooq_by_code)
    if skipped:
        logger.info("突合対象外 %d 銘柄（stooq 未取得 or 同一基準日の行なし §3-1）", len(skipped))

    discrepancies = reconcile.reconcile_prices(theirs_df, ours, close_col="Close")
    logger.info(
        "突合: %d 銘柄比較, 乖離 %d 件 (閾値 %.1f%%)",
        len(ours), len(discrepancies), reconcile.DEFAULT_THRESHOLD_PCT,
    )
    ctx.add_success(len(ours))

    for d in discrepancies:
        # 値は書き換えず データ品質=要確認 のみ更新 (§3-5)
        page_id = page_by_code.get(d.code)
        if not page_id:
            continue
        try:
            ctx.client.update_page(
                page_id, {S.PROP_QUALITY: {"select": {"name": "要確認"}}}
            )
            logger.warning(
                "要確認: %s ours=%.1f theirs=%.1f (%.2f%%)",
                d.code, d.ours, d.theirs, d.deviation_pct,
            )
        except Exception as exc:
            ctx.add_failure(d.code, f"要確認フラグ付与失敗: {exc}")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("第2ソース(stooq)による②終値の突合検証 (週1土曜)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
