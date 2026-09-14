"""tdnet_hourly: やのしん当日分 → ④ + 原本。短信XBRL検出時は ③ へ反映 (§8.2, P3)。

- やのしんAPI失敗時は公式TDnetページパースへフォールバック (§11。実データのみ §3-2)
- 原本(一覧JSON/HTML)を ⑤ へ必ずアップロードしてから ④ を書く (§8.1-4)
- 短信 (classify=短信 かつ XBRL あり) は XBRL zip を取得 → tidy 変換 → ③ upsert
"""

from __future__ import annotations

import json
import logging
from datetime import date
from functools import partial

from ..cloud_store import notion_pages
from ..cloud_store.d1 import D1Error, D1Store
from ..collectors import tdnet_official_fallback, tdnet_yanoshin
from ..convert import convert_artifact, xbrl_to_csv
from ..http import FetchError, fetch
from ..licensing import source_license
from ..models import DisclosureRecord, Provenance, RawArtifact, Source, now_jst
from ..notion import file_upload, upsert
from ..rawstore import save_raw
from ..transform import normalize
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "tdnet_hourly"

# 繁忙日（決算集中日）は1日1000件超もあるため上限を大きく取り、
# 上限到達時は公式ページ全件で補完する（無警告の取りこぼし防止）
YANOSHIN_LIMIT = 1000


def _collect(ctx: JobContext, target_date: date) -> list[tuple[RawArtifact, list[DisclosureRecord], dict[str, str]]]:
    """一覧取得。やのしん → 失敗時は公式ページ (§11)、上限到達時は公式で補完。

    返り値: [(原本, records, doc_id→XBRL URL), ...]（公式は1ページ=1原本 §5.1）
    """
    target = target_date.strftime("%Y%m%d")
    try:
        artifact, records = tdnet_yanoshin.list_disclosures(
            ctx.settings, target, limit=YANOSHIN_LIMIT
        )
        payload = json.loads(artifact.local_path.read_bytes())
        convert_artifact(artifact, "json")  # §5.2 ペア保存 (JSON→CSV+Parquet)
        batches = [(artifact, records, tdnet_yanoshin.xbrl_url_map(payload))]
        if len(records) >= YANOSHIN_LIMIT:
            logger.warning(
                "やのしん一覧が上限 %d 件に到達。公式TDnetページで補完する (§11)",
                YANOSHIN_LIMIT,
            )
            seen = {r.doc_id for r in records}
            for page_artifact, page_records in tdnet_official_fallback.fetch_list_pages(
                ctx.settings, target_date
            ):
                extra = [r for r in page_records if r.doc_id not in seen]
                seen |= {r.doc_id for r in extra}
                batches.append((page_artifact, extra, {}))
        return batches
    except (FetchError, ValueError) as exc:
        logger.warning("やのしんAPI失敗 → 公式TDnetページへフォールバック (§11): %s", exc)
        pages = tdnet_official_fallback.fetch_list_pages(ctx.settings, target_date)
        # 公式一覧から XBRL URL は取得しない（リンク有無のみ）。③反映はEDINET日次で補完
        return [(artifact, records, {}) for artifact, records in pages]


def _process_financial_xbrl(
    ctx: JobContext,
    record: DisclosureRecord,
    xbrl_url: str,
    *,
    master_id: str | None = None,
    master_resolved: bool = False,
    sha_map: dict[str, str] | None = None,
    sha_map_date=None,
) -> None:
    """短信 XBRL → 原本⑤ → tidy → ③ upsert (§8.2)。

    master_resolved=True のとき master_id(事前①マップ由来)を ③ relation に使い、
    内部の find_stock_master_page(① per-record 検索)を省く (§8.3)。
    """
    resp = fetch(xbrl_url)
    artifact = save_raw(
        resp.content,
        source=Source.TDNET,
        datatype="tdnet_xbrl",
        scope=record.code or record.doc_id,
        data_date=record.disclosed_at.date(),
        url=xbrl_url,
        ext="zip",
        license_tag=source_license(Source.TDNET),
        base_dir=ctx.settings.raw_data_dir,
        doc_id=record.doc_id,
    )
    tidy = xbrl_to_csv.xbrl_zip_to_tidy(
        artifact.local_path.read_bytes(), record.code or "", record.doc_id
    )
    xbrl_to_csv.write_tidy(tidy, artifact)
    raw_page_id = ctx.upload_raw(artifact, sha_map=sha_map, sha_map_date=sha_map_date)

    # ⑧ 短信 XBRL の全ファクトをローカル専用ストアへミラー（§7.1, factual-cite=内部利用）。
    ctx.mirror_xbrl_facts(tidy, artifact)

    prov = Provenance(
        source=Source.TDNET,
        license_tag=source_license(Source.TDNET),  # factual-cite (§2.1)
        data_date=record.disclosed_at.date(),
        fetched_at=now_jst(),
        raw_page_id=raw_page_id,
    )
    fin = normalize.tidy_to_financial_record(
        tidy, record.code or "", prov, disclosed_at=record.disclosed_at
    )
    if fin is None:
        logger.info("③レコード生成不可 (決算期末を導出できず): %s", record.doc_id)
        return
    # ① relation: 事前マップが解決済みならそれを使い検索を省く。未解決時のみ per-record。
    if not master_resolved:
        try:
            master_id = (
                upsert.find_stock_master_page(ctx.client, ctx.settings, fin.code)
                if fin.code
                else None
            )
        except Exception as exc:  # noqa: BLE001 - relation 解決失敗は本体を止めない
            master_id = None
            logger.warning(
                "① relation 解決失敗 (master_id=None で続行 doc_id=%s): %s",
                record.doc_id, exc,
            )
    # ③ を Notion とローカルへ独立に書く（双方向フェールセーフ）。両系統失敗は
    # 呼び出し側 (execute) が「短信XBRL→③失敗」として記録する。
    if not ctx.persist(
        fin,
        lambda: upsert.upsert_financial_summary(ctx.client, ctx.settings, fin, master_id),
        label=f"③{record.doc_id}",
    ):
        raise RuntimeError(f"③ を Notion/ローカル両系統に書けず: {record.doc_id}")
    # Cloudflare 正本 (D1 jss_financials)。fin is None は上で return 済み。
    # TDnet 由来なので行の license_tag は factual-cite (§2.1)。
    ctx.cloud_financial_summary(
        fin, doc_id=record.doc_id, raw_sha256=artifact.sha256
    )


def _load_map_guarded(loader, label: str):
    """事前マップを all-or-nothing でロードする。失敗時は ({}, False) で per-record へ。"""
    try:
        return loader(), True
    except Exception as exc:  # noqa: BLE001 - 失敗時は per-record 検索へフォールバック
        logger.warning("%s 事前マップ取得失敗 → per-record 検索にフォールバック: %s", label, exc)
        return {}, False


def execute(ctx: JobContext) -> None:
    target_date = ctx.args.date or now_jst().date()
    batches = _collect(ctx, target_date)

    # ① relation マップ(全件・有界)と ④ dedup マップ(対象日のみ)を1回ずつ事前ロード。
    # 毎時実行は同一日の一覧を丸ごと再処理するため、開示ごとの ① 検索・④ 検索(各1req)が
    # 累積し 30分cap を脅かす。事前マップで per-record 検索を排除する(§8.3)。失敗時は
    # per-record 検索へ degrade(all-or-nothing)。
    master_map, master_map_ok = _load_master_map(ctx)
    disc_entries, disc_map_ok = _load_map_guarded(
        lambda: upsert.load_disclosure_page_entries(
            ctx.client, ctx.settings, disclosed_date=target_date
        ),
        "④",
    )
    disc_skipped = 0
    # ⑤ 重複検索の事前マップ（L-21）。原本ごとの 1 req を対象日 1 回にまとめる。
    # 失敗時は per-record 検索へ（upload 側の既定動作）。
    try:
        sha_map: dict[str, str] | None = file_upload.load_raw_page_map(
            ctx.client, ctx.settings, data_date=target_date
        )
    except Exception as exc:  # noqa: BLE001 - 失敗時は per-record 検索へフォールバック
        logger.warning("⑤ 事前マップ取得失敗 → per-record 検索にフォールバック: %s", exc)
        sha_map = None

    for artifact, records, xbrl_urls in batches:
        # 原本必須: Notion⑤/ローカル⑤ の両系統とも失敗時のみ構造化を書かない (§7.1/§3-3)
        raw_page_id = ctx.upload_raw(artifact, sha_map=sha_map, sha_map_date=target_date)

        # ③ の `core_stocks.id` を 1 回でまとめて解決する (§8.3 と同じ考え方)。
        # 毎時実行は同一日の一覧を丸ごと再処理するので、繁忙日（短信1,000件超）は
        # per-record SELECT が 1 回の実行で 1,000 往復になり 30 分 cap を脅かす。
        # 走査行は変わらない（どちらも code の UNIQUE 索引を引いた分だけ）。
        if ctx.cloud is not None:
            ctx.cloud.prefetch_stock_ids(
                [
                    r.code
                    for r in apply_limit(records, ctx.args.limit)
                    if r.code and _has_financial_xbrl(r, xbrl_urls)
                ]
            )

        for record in apply_limit(records, ctx.args.limit):
            record.provenance.raw_page_id = raw_page_id
            # ① relation 解決: 事前マップ優先(miss は relation 欠落のみ=benign)、未取得時は
            # per-record 検索へ degrade。検索失敗も relation 無しで本体は書く(§3-2)。
            master_id, master_resolved = _resolve_master(
                ctx, record.code, master_map, master_map_ok, record.doc_id
            )
            # ④ dedup を事前マップで省く。date-scoped マップは対象日のレコードにのみ信用
            # できるため、disclosed_at が対象日と一致する場合のみ page_resolved。
            disc_resolved = bool(
                disc_map_ok and record.disclosed_at.date() == target_date
            )
            disc_entry = disc_entries.get(record.doc_id) if disc_resolved else None
            # L-20: 既存行と同値なら再 PATCH を省く（毎時 395 件 → 差分のみ）。
            # ローカル系統には書く（persist の notion 側だけを no-op にする）。
            if disc_entry is not None and upsert.disclosure_matches_page(
                disc_entry[1], record, master_id
            ):
                notion_write = partial(_existing_page_id, disc_entry[0])
                disc_skipped += 1
            else:
                notion_write = partial(
                    upsert.upsert_disclosure,
                    ctx.client, ctx.settings, record, master_id,
                    existing_page_id=disc_entry[0] if disc_entry else None,
                    page_resolved=disc_resolved,
                )
            # ④ を Notion とローカルへ独立に書く（双方向フェールセーフ）
            if ctx.persist(
                record,
                notion_write,
                label=f"④{record.doc_id}",
            ):
                ctx.add_success()
            else:
                ctx.add_failure(record.doc_id, "④: Notion/ローカル両系統に書けず")
                continue

            # 上場廃止/新規上場 開示は ① のライフサイクル状態へ反映 (§ Phase3)。
            # 事前①マップ由来の master_id を渡し、内部の ① 検索を省く。
            if record.doc_type in upsert.LIFECYCLE_DOC_TYPES:
                if not ctx.persist_lifecycle(
                    record,
                    lambda rec=record, mid=master_id, mr=master_resolved: (
                        upsert.apply_disclosure_lifecycle(
                            ctx.client, ctx.settings, rec,
                            master_page_id=mid, master_resolved=mr,
                        )
                    ),
                    label=f"①lifecycle:{record.doc_id}",
                ):
                    ctx.add_failure(
                        record.doc_id, "①ライフサイクル: Notion/ローカル両系統に書けず"
                    )

            if _has_financial_xbrl(record, xbrl_urls):
                try:
                    _process_financial_xbrl(
                        ctx, record, xbrl_urls[record.doc_id],
                        master_id=master_id, master_resolved=master_resolved,
                        sha_map=sha_map, sha_map_date=target_date,
                    )
                except Exception as exc:
                    # ③ 反映失敗は欠損として記録 (④ は成立済み。ダミーで埋めない §3-1)
                    ctx.add_failure(record.doc_id, f"短信XBRL→③失敗: {exc}")

    if disc_skipped:
        logger.info("④ 同値 skip: %d 件の再 PATCH を省いた", disc_skipped)


def _existing_page_id(pid: str) -> str:
    """同値 skip 時の notion 書き込み（何も書かず既存 page_id を返す）。"""
    return pid


def _load_master_map(ctx: JobContext) -> tuple[dict[str, str], bool]:
    """① マップを D1 写しから読む。無ければ Notion スキャンへ（L-20）。

    毎時 39 req のスキャンを 1 SELECT に置き換える。写しが空・表が無い
    （本番 DDL 前）・D1 未設定なら従来のスキャンへフォールバックする。
    """
    settings = ctx.settings.cloud_store
    if settings.d1_enabled():
        try:
            mapping = notion_pages.load_stock_master_map(
                D1Store(settings, writer=JOB_NAME)
            )
        except D1Error as exc:
            logger.warning("① D1 写しを読めない → Notion スキャンにフォールバック: %s", exc)
        else:
            if mapping:
                logger.info("① マップ: D1 写し %d 件（スキャンを省いた）", len(mapping))
                return mapping, True
            logger.warning("① D1 写しが空 → Notion スキャンにフォールバック")
    return _load_map_guarded(
        lambda: upsert.load_stock_master_map(ctx.client, ctx.settings), "①"
    )


def _has_financial_xbrl(record: DisclosureRecord, xbrl_urls: dict[str, str]) -> bool:
    """③ へ反映する短信 XBRL を持つ開示か。

    判定を 1 箇所に閉じる。`execute` の事前解決と実処理でこの条件が食い違うと、
    先に解決したコードと実際に使うコードがずれて per-code SELECT に落ちる
    （静かに遅くなるだけなので気付けない）。
    """
    return bool(
        record.doc_type == "短信" and record.has_xbrl and record.doc_id in xbrl_urls
    )


def _resolve_master(
    ctx: JobContext, code: str | None, master_map: dict[str, str], master_map_ok: bool, doc_id: str
) -> tuple[str | None, bool]:
    """① relation の (page_id, resolved) を返す。事前マップ優先、未取得時は per-record 検索。

    resolved=True は「マップで確定(値が None でも『① に該当なし』として確定)」を意味し、
    apply_disclosure_lifecycle/_process_financial_xbrl が per-record 検索に戻らないための旗。
    """
    if not code:
        return None, master_map_ok
    if master_map_ok:
        return master_map.get(code), True
    try:
        return upsert.find_stock_master_page(ctx.client, ctx.settings, code), False
    except Exception as exc:  # noqa: BLE001 - relation 解決失敗は本体を止めない
        logger.warning(
            "① relation 解決失敗 (master_id=None で続行 doc_id=%s): %s", doc_id, exc
        )
        return None, False


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("TDnet適時開示 → ④ (+短信XBRL→③) (平日9-19時毎時)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
