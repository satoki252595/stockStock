"""edinet_daily: 当日書類一覧 → XBRL/CSV/PDF取得 → ③④⑤ (§8.2, P3。トラックA)。

- 財務系書類 (有報/訂正有報/四半期/半期) は type=5 CSV を優先取得し、
  無ければ type=1 XBRL をパース (§5.2)
- 全取得単位の原本を ⑤ へ必ずアップロード (§8.1-4)
- EDINET は commercial-ok (出典記載 §2.1)
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import stat
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path

from ..cloud_store import notion_pages
from ..cloud_store.d1 import D1Error, D1Store
from ..collectors import edinet
from ..collectors.edinet_codelist import normalize_sec_code
from ..convert import json_to_parquet, xbrl_to_csv
from ..http import FetchError
from ..licensing import LicenseTag, source_license
from ..models import ConvertStatus, Provenance, RawArtifact, Source, now_jst
from ..notion import file_upload, upsert
from ..transform import normalize
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "edinet_daily"

# cron の予定時刻 (JST)。.github/workflows/edinet_daily.yml の "0 12 * * 1-5" = 21:00 JST。
# GitHub Actions のスケジュール遅延で実際の起動が翌日 JST へずれるため、起動時刻の
# JST 日付をそのまま対象日にしてはならない (2026-08-27〜09-10 の実測で +3.5h〜+9.5h
# 遅延し、11 営業日連続で「翌日」の空一覧を取得し続けた)。
SCHEDULE_HOUR_JST = 21

# 財務数値の抽出対象 (§4): 有報・四半期・半期と、それぞれの訂正報告書。
# 一覧収集だけでなく CSV/XBRL 抽出・kabuMCP 原本引渡しまで同じ経路へ通す。
FINANCIAL_DOC_TYPES = frozenset({
    edinet.DOC_TYPE_ANNUAL_REPORT,
    edinet.DOC_TYPE_ANNUAL_REPORT_AMEND,
    edinet.DOC_TYPE_QUARTERLY_REPORT,
    edinet.DOC_TYPE_QUARTERLY_REPORT_AMEND,
    edinet.DOC_TYPE_SEMIANNUAL_REPORT,
    edinet.DOC_TYPE_SEMIANNUAL_REPORT_AMEND,
})


def _copy_kabumcp_csv(artifact: RawArtifact, doc_id: str, cache_dir: Path) -> str:
    """永続化済み type5 原本を既存 kabuMCP パーサへ渡す。既存ファイルは上書きしない。"""
    if not re.fullmatch(r"S[0-9A-Z]{7}", doc_id):
        raise ValueError("不正な EDINET docID")
    if (
        artifact.source != Source.EDINET
        or artifact.datatype != "csv"
        or artifact.license_tag != LicenseTag.COMMERCIAL_OK
    ):
        raise ValueError("EDINET / csv / commercial-ok の原本のみ連携可能")
    if artifact.url != f"{edinet.EDINET_API_BASE}/documents/{doc_id}?type=5":
        raise ValueError("原本 URL と docID / type=5 が一致しない")
    data = artifact.local_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError("原本 SHA-256 不一致")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if not any(n.lower().endswith(".csv") for n in archive.namelist()):
            raise ValueError("CSV を含まない ZIP")
        if archive.testzip() is not None:
            raise ValueError("ZIP CRC 不一致")

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{doc_id}.zip"
    # 同じディレクトリの一時ファイルを hard-link で公開。rename/replace と異なり、
    # 競合した既存ファイルを上書きしない。途中までの ZIP も読者へ見せない。
    with tempfile.NamedTemporaryFile(dir=cache_dir, prefix=f".{doc_id}-", delete=False) as tmp:
        temporary = Path(tmp.name)
        try:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                # symlink/FIFO 等を追わず、通常ファイルだけ照合する。
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                with os.fdopen(os.open(target, flags), "rb") as existing:
                    if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                        raise ValueError("既存キャッシュが通常ファイルでない")
                    if hashlib.file_digest(existing, "sha256").hexdigest() != artifact.sha256:
                        raise ValueError("同じ docID の既存キャッシュと内容が異なる（上書き拒否）")
                return "unchanged"
            return "created"
        finally:
            temporary.unlink()


def _export_kabumcp_cache(ctx: JobContext, artifact: RawArtifact, doc_id: str) -> None:
    cache_dir = getattr(ctx.args, "kabumcp_cache_dir", None)
    if cache_dir is None:
        return
    if ctx.settings.dry_run:
        logger.info("kabuMCP cache dry-run: 書込スキップ doc_id=%s", doc_id)
        return
    if artifact.datatype == "xbrl":
        ctx.add_failure(f"kabumcp:{doc_id}", "type1 fallback は連携対象外のためスキップ")
        return
    try:
        result = _copy_kabumcp_csv(artifact, doc_id, cache_dir)
        logger.info("kabuMCP cache %s: doc_id=%s", result, doc_id)
    except Exception as exc:  # noqa: BLE001 - 保存済みの Notion/ローカルは巻き戻さない
        ctx.add_failure(f"kabumcp:{doc_id}", f"キャッシュ連携失敗: {exc}")


def _fetch_financial_tidy(
    ctx: JobContext, doc_id: str, code: str, data_date: date | None
):
    """type=5 CSV 優先 → 無ければ type=1 XBRL (§5.2)。(artifact, tidy|None) を返す。

    tidy 変換の失敗では原本を失わない: convert_status=失敗 を記録して
    原本はそのまま ⑤ アップロードに進める (§5.2「変換失敗時も原本保存は成立」)。
    """
    try:
        artifact = edinet.fetch_document(
            ctx.settings, doc_id, 5, code=code, data_date=data_date
        )
        parser = xbrl_to_csv.edinet_csv_zip_to_tidy
    except FetchError:
        artifact = edinet.fetch_document(
            ctx.settings, doc_id, 1, code=code, data_date=data_date
        )
        parser = xbrl_to_csv.xbrl_zip_to_tidy

    tidy = None
    try:
        tidy = parser(artifact.local_path.read_bytes(), code, doc_id)
        xbrl_to_csv.write_tidy(tidy, artifact)
    except Exception:
        logger.exception(
            "tidy 変換失敗 (原本は保全し ⑤ へ。③ 反映はスキップ §5.2): doc_id=%s", doc_id
        )
        artifact.convert_status = ConvertStatus.FAILED
    return artifact, tidy


def _resolve_master_id(
    ctx: JobContext, code: str, master_map: dict[str, str], master_map_ok: bool, doc_id: str
) -> str | None:
    """① relation の page_id を解決する。事前マップ優先、未取得時は per-record 検索。

    マップ miss は relation 欠落のみ(④ 行は書ける。重複は起きない)なので degrade。
    検索失敗も relation 無しで本体は書く(§3-2 双方向フェールセーフ)。
    """
    if not code:
        return None
    if master_map_ok:
        return master_map.get(code)
    try:
        return upsert.find_stock_master_page(ctx.client, ctx.settings, code)
    except Exception as exc:  # noqa: BLE001 - relation 解決失敗は本体を止めない
        logger.warning(
            "① relation 解決失敗 (master_id=None で続行 doc_id=%s): %s", doc_id, exc
        )
        return None


def _process_document(
    ctx: JobContext,
    doc: dict,
    list_page_id: str,
    *,
    master_map: dict[str, str] | None = None,
    master_map_ok: bool = False,
    edinet_map: dict[str, str] | None = None,
    disc_map: dict[str, tuple[str, dict]] | None = None,
    disc_map_ok: bool = False,
    target_date: date | None = None,
    sha_map: dict[str, str] | None = None,
) -> None:
    master_map = master_map or {}
    disc_map = disc_map or {}
    doc_id = doc["docID"]
    code = normalize_sec_code(doc.get("secCode")) or ""
    if not code:
        # 大量保有報告書は発行者の EDINETコードでしか対象会社を辿れない。
        # ① に該当が無ければ code は空のまま（④には残すが relation は張らない）。
        issuer = edinet.issuer_edinet_code(doc)
        if issuer:
            code = (edinet_map or {}).get(issuer, "")
    doc_type_code = str(doc.get("docTypeCode") or "")
    submit = doc.get("submitDateTime") or ""
    data_date: date | None = None
    if submit:
        try:
            data_date = datetime.strptime(submit[:10], "%Y-%m-%d").date()
        except ValueError:
            data_date = None

    doc_raw_page: str | None = None
    tidy = None
    tidy_artifact: RawArtifact | None = None

    # 財務系: CSV/XBRL → tidy 変換版付き原本を ⑤ へ (変換失敗でも原本は上げる §5.2)
    if doc_type_code in FINANCIAL_DOC_TYPES:
        tidy_artifact, tidy = _fetch_financial_tidy(ctx, doc_id, code, data_date)
        doc_raw_page = ctx.upload_raw(tidy_artifact, sha_map=sha_map, sha_map_date=target_date)

    # PDF 原本 (§4 書類一覧の対象すべて)。失敗しても書類処理自体は継続
    try:
        pdf_artifact = edinet.fetch_document(
            ctx.settings, doc_id, 2, code=code, data_date=data_date
        )
        json_to_parquet.convert_artifact(pdf_artifact, "pdf")
        pdf_page = ctx.upload_raw(pdf_artifact, sha_map=sha_map, sha_map_date=target_date)
        doc_raw_page = doc_raw_page or pdf_page
    except (FetchError, file_upload.RawUploadError) as exc:
        logger.warning("PDF取得/UL失敗 (書類処理は継続 doc_id=%s): %s", doc_id, exc)

    # ④ 開示書類 upsert (キー=docID)。原本は書類自身 → 無ければ一覧原本
    record = edinet.to_disclosure_record(doc, raw_page_id=doc_raw_page or list_page_id)
    # ① relation 解決。事前マップがあれば per-record 検索を省く(§8.3)。マップ miss は
    # relation 欠落のみ(重複は起きない)なので benign degrade。マップ未取得時は従来の
    # per-record 検索へフォールバック。
    master_id = _resolve_master_id(
        ctx, record.code, master_map, master_map_ok, doc_id
    )
    # ④ dedup を事前マップで省く。date-scoped マップは対象日のレコードにのみ信用できる
    # ため、disclosed_at が対象日と一致する場合のみ page_resolved（範囲外は per-record
    # 検索＝重複防止）。
    disc_resolved = bool(
        disc_map_ok and target_date is not None
        and record.disclosed_at.date() == target_date
    )
    disc_entry = disc_map.get(doc_id) if disc_resolved else None
    existing_pid = disc_entry[0] if disc_entry else None
    existing_props = disc_entry[1] if disc_entry else None
    # L-20: 既存行と同値なら再 PATCH を省く（tdnet_hourly と同じ）。
    # ローカル系統には書く（persist の notion 側だけを no-op にする）。
    if existing_props is not None and upsert.disclosure_matches_page(
        existing_props, record, master_id
    ):
        notion_write = partial(_existing_page_id, existing_pid)
    else:
        notion_write = partial(
            upsert.upsert_disclosure,
            ctx.client, ctx.settings, record, master_id,
            existing_page_id=existing_pid, page_resolved=disc_resolved,
        )
    # ④ を Notion とローカルへ独立に書く（双方向フェールセーフ）
    if not ctx.persist(
        record,
        notion_write,
        label=f"④{doc_id}",
    ):
        raise RuntimeError(f"④ を Notion/ローカル両系統に書けず: {doc_id}")

    # ⑧ XBRL 全ファクト（定性 textBlock 含む）をローカル専用ストアへミラー（§7.1）。
    # Notion ③ は要約のみのため、有報の非構造化情報はここに保持する。ベストエフォート。
    if tidy is not None and tidy_artifact is not None:
        ctx.mirror_xbrl_facts(tidy, tidy_artifact)

    # ③ 財務サマリ (有報は既定で本決算、四半期は tidy の DEI から導出)
    if tidy is not None and tidy_artifact is not None and code:
        prov = Provenance(
            source=Source.EDINET,
            license_tag=source_license(Source.EDINET),  # commercial-ok (§2.1)
            data_date=data_date,
            fetched_at=now_jst(),
            raw_page_id=tidy_artifact.notion_page_id,
        )
        fin = normalize.tidy_to_financial_record(
            tidy, code, prov,
            disclosure_type="本決算" if doc_type_code in ("120", "130") else None,
            disclosed_at=record.disclosed_at,
        )
        # `fin is not None` は明示ガードにする。`if fin is not None and not
        # ctx.persist(...)` の形だと、Cloudflare への書き込みを「raise の次の行」に
        # 足すと到達不能になり、外側インデントに足すと fin=None を踏む。
        if fin is not None:
            if not ctx.persist(
                fin,
                lambda: upsert.upsert_financial_summary(
                    ctx.client, ctx.settings, fin, master_id
                ),
                label=f"③{doc_id}",
            ):
                raise RuntimeError(f"③ を Notion/ローカル両系統に書けず: {doc_id}")
            # Cloudflare 正本 (D1 jss_financials)。器だけあって 0 行だった表への
            # 唯一の writer。⑤原本とは raw_sha256 で結ぶ。
            ctx.cloud_financial_summary(
                fin, doc_id=doc_id, raw_sha256=tidy_artifact.sha256
            )

    if tidy_artifact is not None:
        _export_kabumcp_cache(ctx, tidy_artifact, doc_id)


def default_target_date(now: datetime) -> date:
    """既定の対象日 = cron の予定日 (起動時刻の JST 日付ではない)。

    予定は毎営業日 SCHEDULE_HOUR_JST 時 (JST)。したがって、それより前に始まった
    実行は「前日の予定分が遅延したもの」であり、対象日は前日である。この規則は
    予定時刻から 24 時間未満の遅延をすべて正しい日へ吸収する (§3-2 欠損を作らない)。
    """
    if now.hour >= SCHEDULE_HOUR_JST:
        return now.date()
    return now.date() - timedelta(days=1)


def execute(ctx: JobContext) -> None:
    started = now_jst()
    target_date = ctx.args.date or default_target_date(started)
    logger.info(
        "対象日 %s (起動 %s JST / 指定=%s)",
        target_date.isoformat(), started.isoformat(timespec="seconds"),
        "あり" if ctx.args.date else "なし",
    )

    # ⑤ 重複検索の事前マップ（L-21）。原本ごとの 1 req を対象日 1 回にまとめる。
    # 失敗時は per-record 検索へ（upload 側の既定動作）。
    try:
        sha_map: dict[str, str] | None = file_upload.load_raw_page_map(
            ctx.client, ctx.settings, data_date=target_date
        )
    except Exception as exc:  # noqa: BLE001 - 失敗時は per-record 検索へフォールバック
        logger.warning("⑤ 事前マップ取得失敗 → per-record 検索にフォールバック: %s", exc)
        sha_map = None

    # 1-4. 書類一覧取得・原本⑤UL（Notion⑤/ローカル⑤ 独立。両系統とも失敗時のみ中止 §7.1/§3-3）
    list_artifact, docs = edinet.list_documents(ctx.settings, target_date)
    json_to_parquet.convert_artifact(list_artifact, "json")
    list_page_id = ctx.upload_raw(list_artifact, sha_map=sha_map, sha_map_date=target_date)

    # 大量保有報告書(350/360)は保有者が提出するため secCode が入らない。実測で
    # 992件中956件(96%)が空で、secCode だけで絞ると ほぼ全て取りこぼしていた。
    # 対象会社は issuerEdinetCode から ① の EDINETコード逆引きで解決する。
    targets = [
        d for d in docs
        if edinet.is_target_document(d) and edinet.has_identifiable_company(d)
    ]
    targets = apply_limit(targets, ctx.args.limit)
    logger.info("対象書類 %d / 一覧 %d 件", len(targets), len(docs))

    if not docs:
        # 0 件は「休場日なら正常・営業日なら異常」で、成功として黙って終えると
        # 対象日ずれ等の事故が検知できない (実績あり)。⑦ に残して可視化する (§3-2)。
        ctx.add_failure(
            f"EDINET一覧_{target_date.isoformat()}",
            "書類一覧が0件。休場日なら正常だが、営業日で continue するなら対象日ずれを疑う",
        )

    # ① relation マップ(全件・有界)と ④ dedup マップ(対象日のみ)を1回ずつ事前ロード。
    # 書類ごとの ① 検索・④ 検索(各1req)を排除する(§8.3。繁忙日=有報集中の timeout 対策)。
    # 取得失敗時は per-record 検索へ degrade(all-or-nothing)。
    # ① は1回のスキャンで {コード: page_id} と {EDINETコード: コード} の両方を作る
    # （大量保有報告書の対象会社解決に後者が要る。追加の API 呼び出しは発生しない）。
    master_map, edinet_map, master_map_ok = _load_master_maps(ctx)
    logger.info("① マップ: 銘柄 %d 件 / EDINETコード逆引き %d 件", len(master_map), len(edinet_map))
    disc_map, disc_map_ok = _load_map_guarded(
        lambda: upsert.load_disclosure_page_entries(
            ctx.client, ctx.settings, disclosed_date=target_date
        ),
        "④",
    )

    for doc in targets:
        try:
            _process_document(
                ctx, doc, list_page_id,
                master_map=master_map, master_map_ok=master_map_ok,
                edinet_map=edinet_map,
                disc_map=disc_map, disc_map_ok=disc_map_ok, target_date=target_date,
                sha_map=sha_map,
            )
            ctx.add_success()
        except Exception as exc:
            ctx.add_failure(doc.get("docID", "?"), f"書類処理失敗: {exc}")


def _existing_page_id(pid: str) -> str:
    """同値 skip 時の notion 書き込み（何も書かず既存 page_id を返す）。"""
    return pid


def _load_master_maps(
    ctx: JobContext,
) -> tuple[dict[str, str], dict[str, str], bool]:
    """① マップ 2 種を D1 写しから読む。無ければ Notion スキャンへ（L-20）。

    EDINET 逆引きも D1 区画 `stock_master_by_edinet` から読む。どちらか一方
    でも空なら両方スキャンで取り直す（1 回のスキャンで両方作れるため）。
    """
    settings = ctx.settings.cloud_store
    if settings.d1_enabled():
        try:
            store = D1Store(settings, writer=JOB_NAME)
            stock = notion_pages.load_stock_master_map(store)
            by_edinet = notion_pages.load_edinet_code_map(store)
        except D1Error as exc:
            logger.warning("① D1 写しを読めない → Notion スキャンにフォールバック: %s", exc)
        else:
            if stock and by_edinet:
                logger.info(
                    "① マップ: D1 写し stock %d 件 / edinet逆引き %d 件（スキャンを省いた）",
                    len(stock), len(by_edinet),
                )
                return stock, by_edinet, True
            logger.warning("① D1 写しが空 → Notion スキャンにフォールバック")
    master_pages: list[dict] = []

    def _load_master() -> dict[str, str]:
        master_pages.clear()
        master_pages.extend(ctx.client.query_database(ctx.settings.db_id("stock_master")))
        return upsert._master_map_from_pages(master_pages)

    master_map, master_map_ok = _load_map_guarded(_load_master, "①")
    edinet_map = upsert._edinet_map_from_pages(master_pages) if master_map_ok else {}
    return master_map, edinet_map, master_map_ok


def _load_map_guarded(loader, label: str):
    """事前マップを all-or-nothing でロードする。失敗時は ({}, False) で per-record へ。"""
    try:
        return loader(), True
    except Exception as exc:  # noqa: BLE001 - 失敗時は per-record 検索へフォールバック
        logger.warning("%s 事前マップ取得失敗 → per-record 検索にフォールバック: %s", label, exc)
        return {}, False


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("EDINET当日書類 → ③④⑤ (毎営業日21:00 JST)")
    parser.add_argument(
        "--kabumcp-cache-dir", type=Path, default=None,
        help="永続化済み type5 CSV ZIP を kabuMCP 用キャッシュへ追加（既定OFF・上書き禁止）",
    )
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
