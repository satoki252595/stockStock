"""supply_daily: 日証金の貸借取引データ → R2 supply/ と D1 jss_supply_latest。

毎営業日、日証金 (taisyaku.jp) の確報 CSV 3 本を取得して銘柄別に組み替える。

- **履歴は R2 のみ** (supply/{code}.json)。日次 210 万行になるため D1 に置かない
  (D1 の 1DB 10GB 上限は申請でも引き上げ不可)。
- D1 jss_supply_latest には最新断面だけを持つ (4,400 × 3 = 約13,200 行で増えない)。
- 日証金は**最新スナップショットしか公開しない**ため、取り逃した日は永久に
  埋まらない。欠測を作らないことが最優先。
- ライセンスは **personal-only**。公開 API・エクスポート・公開 Worker へ流さない。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ..cloud_store.r2 import R2Store
from ..cloud_store.supply import upsert_supply_series
from ..collectors import jsf_margin as jsf
from ..http import FetchError
from ..licensing import LicenseTag
from ..models import Provenance, Source, SupplyRecord, now_jst
from ..rawstore import save_raw
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "supply_daily"

# R2 への系列書き込みは 1 銘柄あたり GET + PUT の2往復で、4,351銘柄では直列だと
# 30分を超える（本番実測）。R2 の制約は「**同一キー**への並行書込 1/秒」であり、
# supply/{code}.json はキーが銘柄数ぶん分散するので並列化しても 429 は出ない。
# 16 は控えめな値（Notion の 2.5req/s のような全体スロットルは R2 に無い）。
R2_WRITE_WORKERS = 16

# D1 の断面。data_type ごとに 1 銘柄 1 行。
_LATEST_COLUMNS = (
    "code", "data_type", "data_date", "isin", "loan_bal", "loan_chg",
    "stock_bal", "stock_chg", "ratio", "turn_days", "r2_key",
    "license_tag", "fetched_at", "quality",
)


def _fetch_and_store(ctx: JobContext, name: str, datatype: str):
    """CSV を取得し ⑤原本として保存する。原本が残らなければ None。"""
    content = jsf.fetch_csv(name)
    artifact = save_raw(
        content,
        source=Source.JSF,
        datatype=datatype,
        scope="ALL",
        data_date=now_jst().date(),
        url=f"{jsf.BASE_URL}/{name}.csv",
        ext="csv",
        license_tag=LicenseTag.PERSONAL_ONLY,
        base_dir=ctx.settings.raw_data_dir,
    )
    ctx.upload_raw(artifact)
    return artifact, content


def _zandaka_points(rows: list[jsf.ZandakaRow]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        loan_chg = (
            row.loan_new - row.loan_repaid
            if row.loan_new is not None and row.loan_repaid is not None
            else None
        )
        stock_chg = (
            row.stock_new - row.stock_repaid
            if row.stock_new is not None and row.stock_repaid is not None
            else None
        )
        point = {
            "d": row.apply_date.isoformat(),
            "ex": row.exchange,
            "loan_bal": row.loan_bal,
            "loan_chg": loan_chg,
            "stock_bal": row.stock_bal,
            "stock_chg": stock_chg,
            "ratio": row.ratio,
            "turn_days": row.turn_days_total,
            "loan_amt": row.loan_bal_amount,
            "stock_amt": row.stock_bal_amount,
        }
        out.setdefault(row.code, []).append({k: v for k, v in point.items() if v is not None})
    return out


def _shina_points(rows: list[jsf.ShinaRow]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        point = {
            "d": row.apply_date.isoformat(),
            "ex": row.exchange,
            "excess": row.excess_shares,
            "max_rate": row.max_rate,
            "today_rate": row.today_rate,
            "prev_rate": row.prev_rate,
            "note": row.note or None,
            "bid_rank": row.bid_rank or None,
        }
        out.setdefault(row.code, []).append({k: v for k, v in point.items() if v is not None})
    return out


# 日証金の CSV は同一銘柄を取引所ごとに別行で出す（実測 4,755 行 / 4,351 銘柄、
# 複数取引所に出るのは 376 銘柄）。D1 の断面は主キーが (code, data_type) なので、
# そのまま全行を送ると**後勝ちで上書き**される。実際トヨタ(7203)は
# 東証=融資残 1,005,100 に対し名証=0 で、名証が東証を潰していた。
# 断面には主取引所の行を選ぶ。全取引所ぶんの明細は R2 の系列に残る。
PRIMARY_EXCHANGE = "東証およびＰＴＳ"


def pick_primary_rows(records: list[SupplyRecord]) -> list[SupplyRecord]:
    """(code, data_type) ごとに主取引所の行を1つ選ぶ。

    東証があればそれ。無ければ（名証・福証・札証の単独上場）**ファイル内の
    最初の行**を採る。金額の大小で選ぶと年によって基準が変わるので、
    公表側の並び順という決定的な規則にする。
    """
    chosen: dict[tuple[str, str], SupplyRecord] = {}
    for record in records:
        key = (record.code, record.data_type)
        current = chosen.get(key)
        if current is None:
            chosen[key] = record
        elif current.exchange != PRIMARY_EXCHANGE and record.exchange == PRIMARY_EXCHANGE:
            chosen[key] = record
    return list(chosen.values())


def _latest_row(record: SupplyRecord, r2_key: str) -> list:
    return [
        record.code, record.data_type, record.data_date.isoformat(), record.isin,
        record.loan_bal, record.loan_chg, record.stock_bal, record.stock_chg,
        record.ratio, record.turn_days, r2_key,
        record.provenance.license_tag.value,
        int(record.provenance.fetched_at.timestamp()),
        record.provenance.quality.value,
    ]


def execute(ctx: JobContext) -> None:
    fetched_at = now_jst()
    by_code: dict[str, dict[str, list[dict]]] = {}
    latest: list[SupplyRecord] = []

    # 1. zandaka (貸借残高・必須)。取れなければジョブの意味が無いので失敗にする。
    try:
        _artifact, content = _fetch_and_store(ctx, "zandaka", "zandaka")
        rows = jsf.parse_zandaka(content)
        logger.info("zandaka: %d 行 / %d 銘柄", len(rows), len({r.code for r in rows}))
        for code, points in _zandaka_points(rows).items():
            by_code.setdefault(code, {})["jsf_zandaka"] = points
        for row in rows:
            latest.append(SupplyRecord(
                code=row.code, data_type="jsf_zandaka", data_date=row.apply_date,
                exchange=row.exchange, loan_bal=row.loan_bal, stock_bal=row.stock_bal,
                ratio=row.ratio, turn_days=row.turn_days_total,
                provenance=Provenance(
                    source=Source.JSF, license_tag=LicenseTag.PERSONAL_ONLY,
                    data_date=row.apply_date, fetched_at=fetched_at,
                ),
            ))
    except FetchError as exc:
        ctx.add_failure("zandaka", f"取得失敗: {exc}")
        return

    # 2. shina (逆日歩・任意)。取れなくても zandaka は書き切る。
    try:
        _artifact, content = _fetch_and_store(ctx, "shina", "shina")
        shina_rows = jsf.parse_shina(content)
        logger.info("shina: %d 行", len(shina_rows))
        for code, points in _shina_points(shina_rows).items():
            by_code.setdefault(code, {})["jsf_shina"] = points
    except FetchError as exc:
        ctx.add_failure("shina", f"取得失敗（zandaka は継続）: {exc}")

    # 3. meigara (貸借銘柄区分・任意)。原本として残すだけで系列には積まない。
    try:
        _fetch_and_store(ctx, "meigara", "meigara")
    except FetchError as exc:
        ctx.add_failure("meigara", f"取得失敗（継続）: {exc}")

    codes = apply_limit(sorted(by_code), ctx.args.limit)
    logger.info("R2 supply/ へ %d 銘柄を書き込む", len(codes))

    cloud = ctx.cloud
    if cloud is None or not cloud.settings.r2_enabled():
        logger.warning("R2 未設定のため系列を書けない（原本のみ保存した）")
        return

    store = R2Store(cloud.settings, cloud.settings.bucket_supply, writer=JOB_NAME)

    def _write_one(code: str) -> tuple[str, str | None, str | None]:
        """(code, key, error) を返す。例外はスレッド内で捕まえて呼び出し側へ渡す。"""
        try:
            return code, upsert_supply_series(
                store, code, by_code[code], updated=fetched_at.date()
            ), None
        except Exception as exc:  # noqa: BLE001 - 1銘柄の失敗で全体を止めない
            return code, None, str(exc)

    keys: dict[str, str] = {}
    # 集計は ThreadPoolExecutor の外（メインスレッド）で行う。ctx のカウンタに
    # ロックが無いため、ワーカーから直接触らない。
    with ThreadPoolExecutor(max_workers=R2_WRITE_WORKERS) as pool:
        for code, key, error in pool.map(_write_one, codes):
            if key is not None:
                keys[code] = key
                ctx.add_success()
            else:
                ctx.add_failure(code, f"R2 supply/ へ書けず: {error}")

    # 4. D1 の最新断面。R2 が書けた銘柄だけ索引する（索引が嘘をつかない）。
    if not cloud.settings.d1_enabled():
        logger.info("D1 未設定のため jss_supply_latest は更新しない")
        return
    rows_to_write = [
        _latest_row(r, keys[r.code])
        for r in pick_primary_rows(latest)
        if r.code in keys
    ]
    try:
        written = cloud.d1.upsert(
            "jss_supply_latest", list(_LATEST_COLUMNS), rows_to_write,
            conflict=["code", "data_type"],
        )
        logger.info("D1 jss_supply_latest: %d 行", written)
    except Exception as exc:  # noqa: BLE001 - R2 に系列は残っている
        ctx.cloud_failed += 1
        logger.warning("D1 jss_supply_latest の更新に失敗（R2 の系列は残っている）: %s", exc)


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("日証金の貸借取引データ → ⑧'需給 (毎営業日 12:00 JST)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
