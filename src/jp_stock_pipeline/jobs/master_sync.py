"""master_sync: EDINETコードリスト → ① 銘柄マスタ同期 (DESIGN.md §8.2, P1)。

フロー (§8.1): fetch → save raw → convert → ⑤UL(必須) → parse → ① upsert → D1 実行記録。
原本(⑤)を Notion・ローカルの両系統に保存できなかった取得単位のみ構造化を書かず中止する
（片系統に原本が残れば構造化は書く。① upsert 自体も Notion/ローカル独立 §7.1/§3-3）。

## D1 `core_stocks.sector33` の充填（updated_at を進めない）

`parse_codelist` が返す `StockMasterRecord.sector33` は EDINET コードリストの
「提出者業種」（commercial-ok）。Notion ① とローカル ① へは原文のまま書き、
**D1 `core_stocks.sector33` へは東証33業種の名称へ正規化してから**書く
（`contracts/sector33.py`。公開面が33業種で表示するため）。

以前はここで充填しなかった。汎用の列充填（旧 `build_column_update`。D-14-1 で
削除）が `updated_at = (unixepoch())` を進めるので、月次に充填すると
`cloud_store/datasets.py` が `core_stocks` の鮮度に使う `MAX(updated_at)` が
**毎月必ず進み**、kabulab-cf の月次 universe sync が死んでいても SLO が発火
しなくなるからである（JPX の 404 で銘柄マスタが 33 日止まったのに誰も気づか
なかった事象を、自分の書き込みで隠す）。

**`sector33` だけを SET する専用の UPDATE**
（`core_stocks.build_sector33_updates`）で解いた。`updated_at` を
進めるのは kabulab-cf だけのままなので、鮮度の意味は変わらない。詳細は
`cloud_store/core_stocks.py` の「sector33 の充填で updated_at を進めない理由」。

- **差分だけ書く。** `SELECT code, sector33 FROM core_stocks` を 1 文読み、
  値が変わる行だけ UPDATE する。初回 backfill の後は通常 0 文
- **コードリストに現れない銘柄は触らない**（一時的な欠落で既存値を NULL に潰さない）
- `--dry-run` は読むだけで書かない。`--limit` は部分取得なので読みもしない
- D1 の失敗は `ctx.add_failure` に記録し、Notion / ローカルの同期は止めない
- `ctx.cloud` を経由しない。runner は dry-run で `ctx.cloud` を張らないので、
  依存すると dry-run で件数を出せない（`freshness_probe` と同じ）
"""

from __future__ import annotations

import logging
import time
from functools import partial

from ..cloud_store import core_stocks, notion_pages
from ..cloud_store.d1 import D1Error, D1Store
from ..collectors import edinet_codelist
from ..notion import upsert
from .runner import JobContext, apply_limit, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "master_sync"

# 上場廃止検知の安全弁: 既存①に対しコードリストが極端に縮んだ取得は異常とみなし、
# 一括上場廃止を防ぐ (§ Phase3 blast-radius)。取得コード数が既存の50%未満なら中止。
MIN_CODELIST_COVERAGE = 0.5


def execute(ctx: JobContext) -> None:
    # 1-3. Fetch / Save raw / Convert
    artifact = edinet_codelist.fetch_codelist(ctx.settings)
    artifact = edinet_codelist.convert_codelist(artifact)

    # 4. ⑤へ原本+変換版を UL（Notion⑤/ローカル⑤ 独立）。両系統とも保存に失敗した
    #    ときのみ RawUploadError を伝播し中止（片系統に残れば構造化は続行 §7.1/§3-3）
    raw_page_id = ctx.upload_raw(artifact)

    # 5. Transform（全件。上場廃止検知のため limit 前の全コードを保持）
    records = edinet_codelist.parse_codelist(
        artifact.local_path.read_bytes(), raw_page_id=raw_page_id
    )
    fetched_codes = {r.code for r in records}
    # 同一コードの重複を除去（事前マップ + page_resolved 運用では、同一 run 内に同一
    # コードが複数あると未収録キーが二重 create されうる。コードリストはコード一意の
    # はずだが防御的に潰す。最後の出現を採用）。
    upsert_records = _dedup_by_code(apply_limit(records, ctx.args.limit))
    logger.info("コードリスト: %d 銘柄を ① へ upsert", len(upsert_records))

    # ① 既存行マップ {code: page_id} を一括取得（per-record 検索を排除 §8.3。
    # ~3900銘柄×1req削減）。取得失敗時は per-record 検索へ degrade（all-or-nothing:
    # 部分マップを信用して create すると重複行になるため §8.1-6）。このマップは
    # 上場廃止検知でも再利用し ① 全件スキャンを 2回→1回 にする。
    try:
        master_entries = upsert.load_stock_master_entries(ctx.client, ctx.settings)
        map_ok = True
    except Exception as exc:  # noqa: BLE001 - 失敗時は per-record 検索へフォールバック
        master_entries, map_ok = {}, False
        logger.warning("① マップ取得失敗 → per-record 検索にフォールバック: %s", exc)
    master_map = {code: pid for code, (pid, _props) in master_entries.items()}

    # 6. Upsert (冪等キー=銘柄コード)。状態/上場日/上場廃止日 は開示・消失が所有する
    #    ため codelist 同期では書かない (include_lifecycle=False § Phase3 二重所有回避)。
    def _existing_page_id(pid: str) -> str:
        """同値 skip 時の notion 書き込み（何も書かず既存 page_id を返す）。"""
        return pid

    skipped = 0
    for record in upsert_records:
        # L-19: 既存行と同値なら PATCH を省く（月次 3,841 件 → 差分のみ）。
        # ローカル系統には書く（persist の notion 側だけを no-op にする）。
        entry = master_entries.get(record.code)
        if map_ok and entry is not None and upsert.stock_master_matches_page(entry[1], record):
            skipped += 1
            notion_write = partial(_existing_page_id, entry[0])
        else:
            notion_write = partial(
                upsert.upsert_stock_master,
                ctx.client,
                ctx.settings,
                record,
                include_lifecycle=False,
                existing_page_id=master_map.get(record.code),
                page_resolved=map_ok,
            )
        # Notion とローカルへ独立に書く（双方向フェールセーフ）。状態/上場日/廃止日は
        # 開示・消失が所有するため include_lifecycle=False で両系統とも書かない。
        if ctx.persist(
            record,
            notion_write,
            label=f"①{record.code}",
            include_lifecycle=False,
        ):
            ctx.add_success()
        else:
            ctx.add_failure(record.code, "①: Notion/ローカル両系統に書けず")
    if skipped:
        logger.info("① 同値 skip: %d 件の PATCH を省いた", skipped)

    # 7. 上場廃止検知 (§ Phase3): コードリストから消えた銘柄を listed=False にする
    #    (状態=上場廃止 の確定は一次開示が所有)。--limit 指定時は部分取得のため
    #    誤判定回避でスキップする。
    # 8. D1 core_stocks.sector33 の充填も --limit では行わない。差分は「コードリストに
    #    居る銘柄」だけを対象にするので部分取得でも既存値は潰れないが、部分取得で
    #    本番へ書くと「一部だけ最新」の状態を作るため、上場廃止検知と同じ扱いにする。
    if ctx.args.limit:
        logger.info("--limit 指定のため上場廃止検知はスキップ (部分取得 § Phase3)")
        logger.info("--limit 指定のため D1 core_stocks.sector33 の充填はスキップ (部分取得)")
        return
    _detect_delistings(ctx, fetched_codes, master_map, map_ok)
    _sync_sector33(ctx, records)
    _sync_notion_pages(ctx, master_entries, map_ok)


def _sector33_store(ctx: JobContext) -> D1Store | None:
    """`core_stocks` がある D1（正本）への接続。資格情報が無ければ None。"""
    settings = ctx.settings.cloud_store
    if not settings.d1_enabled():
        return None
    return D1Store(settings, writer=JOB_NAME)


def _sync_notion_pages(
    ctx: JobContext, master_entries: dict[str, tuple[str, dict]], map_ok: bool
) -> None:
    """① の {コード: page_id} 写しを D1 へ書く（L-20。読むのは tdnet/edinet）。

    同じスキャンから EDINET 逆引きも写す（追加の req は出ない）。
    `--dry-run` / `--limit` では書かない（sector33 と同じ扱い）。
    マップ取得に失敗していたら書かない（`{}` で上書きしない）。
    D1 の失敗は記録だけして同期は止めない（写しが古くても読み手は
    Notion スキャンへフォールバックする）。
    """
    if ctx.settings.dry_run or ctx.args.limit:
        return
    if not map_ok or not master_entries:
        return
    store = _sector33_store(ctx)
    if store is None:
        return
    stock_map = {code: pid for code, (pid, _props) in master_entries.items()}
    edinet_map = {
        edinet_code: code
        for code, (_pid, props) in master_entries.items()
        if (edinet_code := upsert._edinet_code_of(props))
    }
    try:
        now = int(time.time())
        n_stock = notion_pages.save_map(
            store, notion_pages.DB_STOCK_MASTER, stock_map, updated_at=now
        )
        n_edinet = notion_pages.save_map(
            store, notion_pages.DB_STOCK_MASTER_BY_EDINET, edinet_map, updated_at=now
        )
    except D1Error as exc:
        ctx.add_failure("jss_notion_pages", f"D1 写しを書けない: {exc}")
        return
    logger.info("① D1 写し: stock %d 件 / edinet逆引き %d 件", n_stock, n_edinet)


def _sync_sector33(ctx: JobContext, records: list) -> None:
    """EDINET「提出者業種」で D1 `core_stocks.sector33` の差分だけを埋める。

    D1 未設定は失敗にしない。このジョブの主目的は Notion ① / ローカル ① の同期で、
    D1 の資格情報が無い環境（手元・Notion 単独運用）でも従来どおり成功させる。
    成否は `processed` に数えない（① の件数の意味を変えないため）。
    """
    store = _sector33_store(ctx)
    if store is None:
        logger.info("D1 未設定のため core_stocks.sector33 の充填はスキップ")
        return
    codelist = [(r.code, r.sector33) for r in _dedup_by_code(records)]
    try:
        current = store.query(core_stocks.SECTOR33_SNAPSHOT_SQL)
    except D1Error as exc:
        ctx.add_failure("core_stocks.sector33", f"現在値を読めない: {exc}")
        return
    changes = core_stocks.plan_sector33_updates(current, codelist)
    statements = core_stocks.build_sector33_updates(changes)
    to_null = sum(1 for v in changes.values() if v is None)
    if ctx.settings.dry_run:
        logger.info(
            "dry-run: core_stocks.sector33 を書かない（変わる行 %d 件 / うち NULL %d 件"
            " / UPDATE %d 文 / 読んだ行 %d 件）",
            len(changes), to_null, len(statements), len(current),
        )
        return
    written = 0
    for sql, params in statements:
        try:
            store.query(sql, params)
        except D1Error as exc:
            # 途中の文で止める。書けた分は残るが、次回は差分から再計算するので収束する。
            ctx.add_failure(
                "core_stocks.sector33",
                f"UPDATE 失敗（{written}/{len(changes)} 行まで適用済み）: {exc}",
            )
            return
        written += len(params) - 1
    logger.info(
        "core_stocks.sector33: %d 行を更新（うち NULL %d 件 / %d 文 / 読んだ行 %d 件）",
        written, to_null, len(statements), len(current),
    )


def _dedup_by_code(records: list) -> list:
    """同一銘柄コードの重複レコードを除去する（最後の出現を採用、順序は初出を維持）。"""
    out: dict[str, object] = {}
    for record in records:
        out[record.code] = record
    return list(out.values())


def _detect_delistings(
    ctx: JobContext, fetched_codes: set[str], existing: dict[str, str], map_ok: bool
) -> None:
    """① にあってコードリストから消えた銘柄を listed=False にする (§ Phase3)。

    EDINET 上場区分が非上場へ変わった＝取得停止の信号。listed のみ更新し、
    状態=上場廃止 の確定は一次開示 (apply_disclosure_lifecycle) に一本化する
    (コードリストの一時的揺らぎで誤った権威的状態を書かない §3-1/§3-7)。
    再上場時は次回 upsert が listed=True へ自己修復する。
    dry-run は ① クエリが空のため no-op。

    existing は execute() が upsert 前に取得済みの ① 全行マップを再利用する
    （全件スキャンを 2回→1回 に削減 §8.3）。map_ok=False（マップ取得失敗）の場合は
    既存集合を信用できないため検知を見送る（部分情報での誤った一括廃止を防ぐ §3-2）。
    """
    if not map_ok:
        logger.warning("① マップ未取得のため上場廃止検知をスキップ (§3-2)")
        return
    # 安全弁: 取得コードが既存に対し極端に少ない＝異常取得とみなし一括廃止を防ぐ。
    # 1件でも誤って全銘柄を listed=False にすると全ジョブの取得が止まるため (§3-2)。
    if existing and len(fetched_codes) < MIN_CODELIST_COVERAGE * len(existing):
        ctx.add_failure(
            "codelist",
            f"取得 {len(fetched_codes)} 件が既存 {len(existing)} 件の "
            f"{MIN_CODELIST_COVERAGE:.0%} 未満。異常取得とみなし上場廃止検知を中止 (§ Phase3)",
        )
        return
    absent = [(code, pid) for code, pid in existing.items() if code not in fetched_codes]
    if not absent:
        return
    logger.warning(
        "コードリストから消えた %d 銘柄を取得停止(listed=False)にする (§ Phase3)", len(absent)
    )
    for code, page_id in absent:
        # listed=False を Notion とローカルへ独立に反映（双方向フェールセーフ）
        if ctx.persist_mark_absent(
            code,
            lambda pid=page_id: upsert.mark_master_absent_from_codelist(
                ctx.client, ctx.settings, pid
            ),
            label=f"①absent:{code}",
        ):
            ctx.add_success()
        else:
            ctx.add_failure(code, "listed=False: Notion/ローカル両系統に書けず")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("EDINETコードリスト → ① 銘柄マスタ同期 (月1)")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
