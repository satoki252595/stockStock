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
from ..convert import attach_dataframe_parquet, convert_artifact
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
from .runner import JobContext, apply_limit, build_parser, main_exit, parse_codes_arg, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "prices_daily"


def resolve_codes(ctx: JobContext) -> list[str]:
    """対象銘柄の決定: --codes > ①銘柄マスタ > EDINETコードリスト直取得。"""
    codes = parse_codes_arg(ctx.args)
    if codes:
        return apply_limit(codes, ctx.args.limit)

    # ① クエリ(Notion read)。Notion 障害でも EDINET コードリストへ degrade して
    # ローカル PG への ② 書き込みを成立させる（双方向フェールセーフの read 側 §3-2）。
    try:
        pages = ctx.client.query_database(
            ctx.settings.db_id("stock_master"),
            filter={"property": S.MASTER_PROP_LISTED, "checkbox": {"equals": True}},
        )
    except Exception as exc:  # noqa: BLE001 - Notion 断は EDINET コードリストで救済
        logger.warning(
            "① クエリ失敗 → EDINET コードリストから銘柄解決へフォールバック (§3-2): %s", exc
        )
        pages = []
    codes = []
    for page in pages:
        rich = page.get("properties", {}).get(S.MASTER_PROP_CODE, {}).get("rich_text", [])
        if rich:
            codes.append(rich[0].get("plain_text", "").strip())
    codes = [c for c in codes if c]
    if not codes:
        # ① が未投入 / dry-run でトークン無しの場合はコードリストから直接 (実データ §3-2)。
        # この取得も1取得単位なので原本+変換版を ⑤ へ保存する (§5.1, §8.1-4)
        logger.info("① から銘柄を取得できないため EDINET コードリストから解決する")
        artifact = edinet_codelist.fetch_codelist(ctx.settings)
        artifact = edinet_codelist.convert_codelist(artifact)
        ctx.upload_raw(artifact)
        records = edinet_codelist.parse_codelist(
            artifact.local_path.read_bytes(), raw_page_id=artifact.notion_page_id
        )
        codes = [r.code for r in records]
    return apply_limit(sorted(set(codes)), ctx.args.limit)


def _ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """コレクター毎の列名差を compute_technicals の要求形式へ正規化（値不変）。

    日付昇順へ整える（並べ替えのみ・値不変）。compute_technicals と
    has_probable_split が同一の「直近」解釈を共有するため (§ 分割検出の順序一貫性)。
    """
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if "date" not in out.columns:
        out = out.reset_index()
        out.columns = [str(c).lower() for c in out.columns]
    out = out[["date", "open", "high", "low", "close", "volume"]]
    return out.sort_values("date").reset_index(drop=True)


def _build_record(
    code: str,
    df: pd.DataFrame,
    src: Source,
    raw_artifact: RawArtifact,
    valuation: dict | None,
) -> PriceTechnicalRecord:
    ohlcv = _ohlcv(df)
    tech = technicals.compute_technicals(ohlcv)
    # 品質判定 (§ コーポレートアクション Phase1。正直化):
    # - 終値なし → 欠損あり
    # - SMA25 すら出ない＝履歴不足（新規上場等）→ 欠損あり（指標が出揃わない §3-1）。
    #   履歴不足の単日急変を「分割」と断定しないよう、分割判定より先に評価する。
    # - 直近窓に大幅な単日変動（分割/併合の可能性）→ 要確認（自動調整しない §3-5）
    if tech["close"] is None:
        quality = DataQuality.MISSING
    elif tech["sma25"] is None:
        quality = DataQuality.MISSING
    elif technicals.has_probable_split(ohlcv["close"]):
        quality = DataQuality.NEEDS_REVIEW
        logger.info("要確認(%s): 直近に大幅な単日変動（分割/併合の可能性）。テクニカルが分割をまたぐ恐れ", code)
    else:
        quality = DataQuality.OK
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
        # fetch_valuation の出力キー (market_cap/per/pbr/dividend_yield) をそのまま使う。
        # dividend_yield は yfinance 1.x 系で % 単位 (実フィクスチャで確認済み)
        record.per = valuation.get("per")
        record.pbr = valuation.get("pbr")
        record.market_cap = valuation.get("market_cap")
        record.dividend_yield_pct = valuation.get("dividend_yield")
    return record


def execute(ctx: JobContext) -> None:
    codes = resolve_codes(ctx)
    if not codes:
        raise RuntimeError("対象銘柄を解決できない (--codes か ① の投入が必要)")
    logger.info("対象 %d 銘柄", len(codes))

    # 1-3. yfinance 一括取得 (原本+Parquet変換版は fetch 内で生成済み)
    yf_artifact, frames, missing = yfinance_prices.fetch_daily_batch(ctx.settings, codes)

    # 4. 原本必須: ⑤ へ UL（Notion⑤/ローカル⑤ 独立）。両系統とも失敗時のみ
    #    RawUploadError で中止（片系統に原本が残れば構造化は続行 §7.1/§3-3）
    ctx.upload_raw(yf_artifact)

    # フォールバック: yfinance 欠損銘柄は stooq の実データのみ (§3-2)
    stooq_results: dict[str, tuple[RawArtifact, pd.DataFrame]] = {}
    for code in missing:
        try:
            artifact, df = stooq_prices.fetch_daily(ctx.settings, code)
            attach_dataframe_parquet(artifact, df)  # 株価履歴の型付き変換版 (§5.2)
            ctx.upload_raw(artifact)
            stooq_results[code] = (artifact, df)
        except FetchError as exc:
            # 全ソース失敗 → 欠損として記録 (§3-2。前日値コピー等は絶対にしない)
            ctx.add_failure(code, f"yfinance/stooq 両方失敗: {exc}")

    # バリュエーション (取れた銘柄のみ §3-1)。取得単位の原本を ⑤ へ保存してから
    # ② へ書く (§8.1-4)。アップロード失敗時はバリュエーション値を書かない
    valuations: dict[str, dict] = {}
    valuation_artifact: RawArtifact | None = None
    if not getattr(ctx.args, "skip_valuation", False) and frames:
        try:
            raw_vals = yfinance_prices.fetch_valuation(ctx.settings, sorted(frames))
            if raw_vals:
                valuation_artifact = yfinance_prices.save_valuation_raw(ctx.settings, raw_vals)
                convert_artifact(valuation_artifact, "json")
                ctx.upload_raw(valuation_artifact)
                valuations = raw_vals
        except (Exception, file_upload.RawUploadError) as exc:
            valuations = {}
            valuation_artifact = None
            logger.warning("バリュエーション取得/原本UL失敗 (②は価格系のみ更新 §8.1-4): %s", exc)

    # 5-6. テクニカル計算 → ② upsert。①の relation は一括マップで解決 (§8.3 レート対策)。
    # relation マップの Notion read 失敗は degrade（master_id=None で本体は書く）。
    # これにより Notion 断でもローカル PG へ ② を書き切れる（双方向フェールセーフ §3-2）。
    try:
        master_map = upsert.load_stock_master_map(ctx.client, ctx.settings)
    except Exception as exc:  # noqa: BLE001 - relation read 失敗は本体を止めない
        master_map = {}
        logger.warning(
            "① relation マップ取得失敗 → master_id=None で ② を継続 (§3-2): %s", exc
        )
    # ② page マップ {code: page_id} を一括取得し per-record 検索を排除 (§8.3)。①relation
    # 用 master_map とは別物（こちらは ② 自身の page_id でキー=title equals）。取得失敗時は
    # per-record 検索へ degrade（all-or-nothing: 部分マップ信用は ② 重複行を毎日増やす）。
    try:
        price_map = upsert.load_price_page_map(ctx.client, ctx.settings)
        price_map_ok = True
    except Exception as exc:  # noqa: BLE001 - 失敗時は per-record 検索へフォールバック
        price_map, price_map_ok = {}, False
        logger.warning("② page マップ取得失敗 → per-record 検索にフォールバック: %s", exc)

    seen_codes: set[str] = set()
    for code in codes:
        if code in seen_codes:
            continue  # 同一 run 内の重複コードは ② を二重 create しうるため1回のみ処理
        seen_codes.add(code)
        if code in frames:
            src, df, artifact = Source.YFINANCE, frames[code], yf_artifact
        elif code in stooq_results:
            artifact, df = stooq_results[code]
            src = Source.STOOQ
        else:
            continue  # failed_codes に記録済み
        try:
            valuation = valuations.get(code)
            record = _build_record(code, df, src, artifact, valuation)
            extra_raw = (
                [valuation_artifact.notion_page_id]
                if valuation and valuation_artifact and valuation_artifact.notion_page_id
                else None
            )
        except Exception as exc:
            ctx.add_failure(code, f"②レコード生成失敗: {exc}")
            continue
        # Notion とローカルへ独立に書く（双方向フェールセーフ）。
        # ローカルは (code, data_date) で時系列蓄積する。
        if ctx.persist(
            record,
            lambda rec=record, mid=master_map.get(code), extra=extra_raw, pid=price_map.get(
                code
            ): (
                upsert.upsert_price_technical(
                    ctx.client,
                    ctx.settings,
                    rec,
                    mid,
                    extra,
                    existing_page_id=pid,
                    page_resolved=price_map_ok,
                )
            ),
            label=f"②{code}",
        ):
            ctx.add_success()
        else:
            ctx.add_failure(code, "②: Notion/ローカル両系統に書けず")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("株価+テクニカル → ② (毎営業日19:30 JST)")
    parser.add_argument(
        "--skip-valuation", action="store_true", help="PER/PBR等の取得を省略 (高速化)"
    )
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
