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
from datetime import date, datetime
from pathlib import Path

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

# 財務数値の抽出対象 (§4): 有報 120 / 訂正有報 130 / 四半期 140 / 半期 160
FINANCIAL_DOC_TYPES = frozenset({"120", "130", "140", "160"})


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
    disc_map: dict[str, str] | None = None,
    disc_map_ok: bool = False,
    target_date: date | None = None,
) -> None:
    master_map = master_map or {}
    disc_map = disc_map or {}
    doc_id = doc["docID"]
    code = normalize_sec_code(doc.get("secCode")) or ""
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
        doc_raw_page = ctx.upload_raw(tidy_artifact)

    # PDF 原本 (§4 書類一覧の対象すべて)。失敗しても書類処理自体は継続
    try:
        pdf_artifact = edinet.fetch_document(
            ctx.settings, doc_id, 2, code=code, data_date=data_date
        )
        json_to_parquet.convert_artifact(pdf_artifact, "pdf")
        pdf_page = ctx.upload_raw(pdf_artifact)
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
    # ④ を Notion とローカルへ独立に書く（双方向フェールセーフ）
    if not ctx.persist(
        record,
        lambda: upsert.upsert_disclosure(
            ctx.client, ctx.settings, record, master_id,
            existing_page_id=disc_map.get(doc_id), page_resolved=disc_resolved,
        ),
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
        if fin is not None and not ctx.persist(
            fin,
            lambda: upsert.upsert_financial_summary(
                ctx.client, ctx.settings, fin, master_id
            ),
            label=f"③{doc_id}",
        ):
            raise RuntimeError(f"③ を Notion/ローカル両系統に書けず: {doc_id}")

    if tidy_artifact is not None:
        _export_kabumcp_cache(ctx, tidy_artifact, doc_id)


def execute(ctx: JobContext) -> None:
    target_date = ctx.args.date or now_jst().date()

    # 1-4. 書類一覧取得・原本⑤UL（Notion⑤/ローカル⑤ 独立。両系統とも失敗時のみ中止 §7.1/§3-3）
    list_artifact, docs = edinet.list_documents(ctx.settings, target_date)
    json_to_parquet.convert_artifact(list_artifact, "json")
    list_page_id = ctx.upload_raw(list_artifact)

    targets = [d for d in docs if edinet.is_target_document(d) and edinet.has_sec_code(d)]
    targets = apply_limit(targets, ctx.args.limit)
    logger.info("対象書類 %d / 一覧 %d 件", len(targets), len(docs))

    # ① relation マップ(全件・有界)と ④ dedup マップ(対象日のみ)を1回ずつ事前ロード。
    # 書類ごとの ① 検索・④ 検索(各1req)を排除する(§8.3。繁忙日=有報集中の timeout 対策)。
    # 取得失敗時は per-record 検索へ degrade(all-or-nothing)。
    master_map, master_map_ok = _load_map_guarded(
        lambda: upsert.load_stock_master_map(ctx.client, ctx.settings), "①"
    )
    disc_map, disc_map_ok = _load_map_guarded(
        lambda: upsert.load_disclosure_page_map(
            ctx.client, ctx.settings, disclosed_date=target_date
        ),
        "④",
    )

    for doc in targets:
        try:
            _process_document(
                ctx, doc, list_page_id,
                master_map=master_map, master_map_ok=master_map_ok,
                disc_map=disc_map, disc_map_ok=disc_map_ok, target_date=target_date,
            )
            ctx.add_success()
        except Exception as exc:
            ctx.add_failure(doc.get("docID", "?"), f"書類処理失敗: {exc}")


def _load_map_guarded(loader, label: str) -> tuple[dict[str, str], bool]:
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
