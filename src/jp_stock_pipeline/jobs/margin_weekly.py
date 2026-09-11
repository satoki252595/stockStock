"""margin_weekly: JPX 信用残 PDF → R2 `vwap-data/margin/` (移行 P2)。

kabulab-cf の `vwap-ingest.yml` が担っていた writer を stockStock へ移管する。
**読み手（kabulab-cf の `/vwap-analysis/api/margin`）から見て出力が1バイトも
変わらない**ことが要件。

## 2026-09-28 の様式変更

JPX は 2026-09-28 から週次→毎営業日16:00へ変更する。旧様式のパースは実装済みで、
新様式は公開後に実データを見てから追加する。`detect_layout()` が判定できない
様式は**推測せず失敗として記録**し、その日は書かない（欠測として扱う。§3-1）。

## 既存週の不可侵

既存10週のうち 2026-06-12〜07-31 は JPX が公開を終えており再取得できない。
既存の `margin/{date}.json` は既定で上書きしない。
"""

from __future__ import annotations

import logging

from ..cloud_store.margin import update_weeks_index, write_margin_snapshot
from ..cloud_store.r2 import R2Store
from ..collectors import jpx_margin as jm
from ..http import FetchError, fetch
from ..licensing import LicenseTag
from ..models import Source
from ..rawstore import save_raw
from .runner import JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "margin_weekly"


def _fetch_index(ctx: JobContext) -> str:
    resp = fetch(jm.INDEX_URL, headers={"User-Agent": jm.USER_AGENT})
    return resp.text


def _fetch_pdf(ctx: JobContext, url: str) -> bytes:
    resp = fetch(url, headers={"User-Agent": jm.USER_AGENT})
    content = resp.content
    if not content.startswith(b"%PDF"):
        raise FetchError(f"JPX 信用残が PDF でない: {url} head={content[:16]!r}")
    return content


def execute(ctx: JobContext) -> None:
    try:
        index_html = _fetch_index(ctx)
        url = jm.latest_pdf_url(index_html)
    except (FetchError, ValueError) as exc:
        ctx.add_failure("index", f"一覧ページから PDF URL を特定できず: {exc}")
        return
    logger.info("対象 PDF: %s", url)

    try:
        pdf_bytes = _fetch_pdf(ctx, url)
    except FetchError as exc:
        ctx.add_failure(url, f"PDF 取得失敗: {exc}")
        return

    text = jm.extract_pdf_text(pdf_bytes)
    layout = jm.detect_layout(text)
    if layout is jm.Layout.UNKNOWN:
        # 2026-09-28 の様式変更で未知の形になった場合はここに落ちる。
        # 推測でパースせず、その日は書かない（欠測として記録 §3-1/§3-2）。
        ctx.add_failure(url, "PDF の様式を判定できない（新様式の可能性）。この回は書き込まない")
        return
    if layout is jm.Layout.DAILY:
        ctx.add_failure(
            url,
            "2026-09-28 以降の日次様式は未実装。実データ公開後にパーサを追加すること",
        )
        return

    data = jm.parse_margin_text(text)
    if not data.week or not data.rows:
        ctx.add_failure(url, f"解析結果が空（week={data.week!r} rows={len(data.rows)}）")
        return
    logger.info("解析: week=%s rows=%d", data.week, len(data.rows))

    # ⑤原本として保存（R2 へも流れる）。personal-only。
    artifact = save_raw(
        pdf_bytes,
        source=Source.JPX,
        datatype="margin_weekly",
        scope="ALL",
        data_date=None,
        url=url,
        ext="pdf",
        license_tag=LicenseTag.PERSONAL_ONLY,
        base_dir=ctx.settings.raw_data_dir,
    )
    ctx.upload_raw(artifact)

    cloud = ctx.cloud
    if cloud is None or not cloud.settings.r2_enabled():
        logger.warning("R2 未設定のため margin/ は書けない（原本のみ保存した）")
        return

    # 互換シムは既存バケット vwap-data に書く（kabulab-cf の読み手がここを見る）。
    store = R2Store(cloud.settings, cloud.settings.bucket_timeseries, writer=JOB_NAME)

    if getattr(ctx.args, "check_only", False):
        # 切替前の確認用。**1バイトも書かない**で現状と差分だけを報告する。
        _report_check_only(ctx, store, data)
        return

    try:
        key, written = write_margin_snapshot(store, data)
        if not written:
            logger.info("margin: %s は既存のため書き込みなし", key)
        weeks = update_weeks_index(store, data.week)
        logger.info("margin: weeks.json = %d 件 (最新 %s)", len(weeks), weeks[-1])
        ctx.add_success()
    except Exception as exc:  # noqa: BLE001 - 失敗は欠測として記録し握りつぶさない
        ctx.add_failure(data.week, f"R2 margin/ へ書けず: {exc}")


def _report_check_only(ctx: JobContext, store: "R2Store", data: "jm.MarginData") -> None:
    """書かずに現状との差分を報告する（切替前ゲートの確認用）。"""
    from ..cloud_store.margin import WEEKS_KEY, merge_weeks, snapshot_key

    weeks, _found = store.get_json(WEEKS_KEY)
    weeks = weeks if isinstance(weeks, list) else []
    key = snapshot_key(data.week)
    existing, snapshot_found = store.get_json(key)

    logger.info("=== check-only（書き込みなし） ===")
    logger.info("解析できた週: %s / 行数: %d", data.week, len(data.rows))
    logger.info("weeks.json 現在: %d 件 (最新 %s)", len(weeks), weeks[-1] if weeks else None)
    logger.info("この週が weeks.json に既にあるか: %s", data.week in weeks)
    logger.info("%s の存在: %s", key, snapshot_found)
    if snapshot_found:
        same = existing == data.to_dict()
        logger.info("既存スナップショットと解析結果が一致するか: %s", same)
        if not same:
            old_rows = len((existing or {}).get("rows") or [])
            logger.warning("  既存 rows=%d / 解析 rows=%d", old_rows, len(data.rows))
    logger.info("書き込んだ場合の weeks.json: %d 件", len(merge_weeks(weeks, data.week)))
    ctx.add_success()


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("JPX 信用残 → R2 vwap-data/margin (移行 P2)")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="R2 へ1バイトも書かず、現状との差分だけを報告する（切替前ゲート確認用）",
    )
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
