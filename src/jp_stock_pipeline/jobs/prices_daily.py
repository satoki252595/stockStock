"""prices_daily: yfinance一括 → テクニカル計算 → ② upsert (DESIGN.md §8.2, P2)。

- yfinance 失敗銘柄は stooq の実データへフォールバック (§3-2)。
  両方失敗した銘柄は欠損として failed_codes に記録（ダミー埋め禁止 §3-1）
- 原本（一括取得単位の CSV + Parquet 変換版）を ⑤ へ必ずアップロード (§8.1-4)
- 計算指標のライセンスタグは入力価格ソースから licensing.inherit() で継承
  (= personal-only §2.2)
"""

from __future__ import annotations

import logging

import pandas as pd

from ..collectors import edinet_codelist, stooq_prices, yfinance_prices
from ..http import FetchError
from ..licensing import inherit, source_license
from ..models import (
    DataQuality,
    PriceTechnicalRecord,
    Provenance,
    RawArtifact,
    Source,
    now_jst,
)
from ..notion import file_upload, upsert
from ..notion import schema as S
from ..transform import technicals
from ..transform.normalize import yf_info_to_valuation
from .runner import JobContext, apply_limit, build_parser, main_exit, parse_codes_arg, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "prices_daily"


def resolve_codes(ctx: JobContext) -> list[str]:
    """対象銘柄の決定: --codes > ①銘柄マスタ > EDINETコードリスト直取得。"""
    codes = parse_codes_arg(ctx.args)
    if codes:
        return apply_limit(codes, ctx.args.limit)

    pages = ctx.client.query_database(
        ctx.settings.db_id("stock_master"),
        filter={"property": S.MASTER_PROP_LISTED, "checkbox": {"equals": True}},
    )
    codes = []
    for page in pages:
        rich = page.get("properties", {}).get(S.MASTER_PROP_CODE, {}).get("rich_text", [])
        if rich:
            codes.append(rich[0].get("plain_text", "").strip())
    codes = [c for c in codes if c]
    if not codes:
        # ① が未投入 / dry-run でトークン無しの場合はコードリストから直接 (実データ §3-2)
        logger.info("① から銘柄を取得できないため EDINET コードリストから解決する")
        artifact = edinet_codelist.fetch_codelist(ctx.settings)
        records = edinet_codelist.parse_codelist(artifact.local_path.read_bytes())
        codes = [r.code for r in records]
    return apply_limit(sorted(set(codes)), ctx.args.limit)


def _ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """コレクター毎の列名差を compute_technicals の要求形式へ正規化（値不変）。"""
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if "date" not in out.columns:
        out = out.reset_index()
        out.columns = [str(c).lower() for c in out.columns]
    return out[["date", "open", "high", "low", "close", "volume"]]


def _build_record(
    code: str,
    df: pd.DataFrame,
    src: Source,
    raw_artifact: RawArtifact,
    valuation: dict | None,
) -> PriceTechnicalRecord:
    ohlcv = _ohlcv(df)
    tech = technicals.compute_technicals(ohlcv)
    quality = DataQuality.OK if tech["close"] is not None else DataQuality.MISSING
    prov = Provenance(
        source=src,
        # テクニカルは計算値: 入力価格ソースのタグを継承 (§2.2) = personal-only
        license_tag=inherit([source_license(src)]),
        data_date=pd.to_datetime(ohlcv["date"].iloc[-1]).date(),
        fetched_at=now_jst(),
        raw_page_id=raw_artifact.notion_page_id,
        quality=quality,
    )
    record = PriceTechnicalRecord(code=code, provenance=prov, **tech)
    if valuation:
        record.per = valuation.get("per")
        record.pbr = valuation.get("pbr")
        record.market_cap = valuation.get("market_cap")
        record.dividend_yield_pct = valuation.get("dividend_yield_pct")
    return record


def execute(ctx: JobContext) -> None:
    codes = resolve_codes(ctx)
    if not codes:
        raise RuntimeError("対象銘柄を解決できない (--codes か ① の投入が必要)")
    logger.info("対象 %d 銘柄", len(codes))

    # 1-3. yfinance 一括取得 (原本+Parquet変換版は fetch 内で生成済み)
    yf_artifact, frames, missing = yfinance_prices.fetch_daily_batch(ctx.settings, codes)

    # 4. 原本必須: ⑤ へアップロード (失敗時 RawUploadError → 構造化書き込みなしで異常終了)
    file_upload.upload_raw_artifact(ctx.client, ctx.settings, yf_artifact)

    # フォールバック: yfinance 欠損銘柄は stooq の実データのみ (§3-2)
    stooq_results: dict[str, tuple[RawArtifact, pd.DataFrame]] = {}
    for code in missing:
        try:
            artifact, df = stooq_prices.fetch_daily(ctx.settings, code)
            file_upload.upload_raw_artifact(ctx.client, ctx.settings, artifact)
            stooq_results[code] = (artifact, df)
        except FetchError as exc:
            # 全ソース失敗 → 欠損として記録 (§3-2。前日値コピー等は絶対にしない)
            ctx.add_failure(code, f"yfinance/stooq 両方失敗: {exc}")

    # バリュエーション (取れた銘柄のみ §3-1)
    valuations: dict[str, dict] = {}
    if not getattr(ctx.args, "skip_valuation", False) and frames:
        try:
            raw_vals = yfinance_prices.fetch_valuation(ctx.settings, sorted(frames))
            valuations = {c: yf_info_to_valuation(v) for c, v in raw_vals.items()}
        except Exception as exc:
            logger.warning("バリュエーション取得失敗 (②は価格系のみ更新): %s", exc)

    # 5-6. テクニカル計算 → ② upsert
    for code in codes:
        if code in frames:
            src, df, artifact = Source.YFINANCE, frames[code], yf_artifact
        elif code in stooq_results:
            artifact, df = stooq_results[code]
            src = Source.STOOQ
        else:
            continue  # failed_codes に記録済み
        try:
            record = _build_record(code, df, src, artifact, valuations.get(code))
            master_id = upsert.find_stock_master_page(ctx.client, ctx.settings, code)
            upsert.upsert_price_technical(ctx.client, ctx.settings, record, master_id)
            ctx.add_success()
        except Exception as exc:
            ctx.add_failure(code, f"②upsert失敗: {exc}")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("株価+テクニカル → ② (毎営業日19:30 JST)")
    parser.add_argument(
        "--skip-valuation", action="store_true", help="PER/PBR等の取得を省略 (高速化)"
    )
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
