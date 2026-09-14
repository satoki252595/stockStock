"""ジョブ共通基盤 (DESIGN.md §8.1 共通フロー / §10 dry-run 必須 / §8.1-7 ⑦記録)。

- 全ジョブ: --dry-run / --date YYYY-MM-DD / --limit N / --codes に対応
- dry-run では Notion へ一切書き込まない（client が記録のみ §3-6）。
  未設定の DB ID は合成ID ("dry-run-db-*") で埋め、設定なしでも完走できる
- 終了時に ⑦ 収集ジョブログへ 1 行記録（成功 / 一部失敗 / 失敗）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from ..config import (
    DB_REGISTRY,
    DB_TARGET_CLOUD,
    DB_TARGETS,
    Settings,
    load_settings,
)
from ..local_store import connect_local_store
from ..notion.client import NotionClient

if TYPE_CHECKING:
    from ..cloud_store.sink import CloudSink
    from ..local_store import LocalStore

logger = logging.getLogger(__name__)

STATUS_SUCCESS = "成功"
STATUS_PARTIAL = "一部失敗"
STATUS_FAILURE = "失敗"


@dataclass
class JobContext:
    settings: Settings
    client: NotionClient
    args: argparse.Namespace
    processed: int = 0
    failed: int = 0
    failed_codes: list[str] = field(default_factory=list)
    local: LocalStore | None = None  # dual-write 先 (未設定/接続不可なら None)
    cloud: "CloudSink | None" = None  # Cloudflare 正本 (未設定なら None)
    mirror_failed: int = 0  # ローカルミラー失敗数 (片系統失敗はジョブ成否に影響させない)
    notion_failed: int = 0  # Notion 書き込み失敗数 (双方向フェールセーフ用。同上)
    cloud_failed: int = 0  # Cloudflare 書き込み失敗数 (同上)

    def add_success(self, n: int = 1) -> None:
        self.processed += n

    def add_failure(self, code: str, reason: str = "") -> None:
        """欠損・取得失敗は隠さず記録する (§3-2)。"""
        self.failed += 1
        self.failed_codes.append(code)
        logger.warning("失敗: %s %s", code, reason)

    # --- Notion ↔ ローカル PostgreSQL の双方向フェールセーフ dual-write --------
    # 方針: Notion とローカルは独立に書き込み、片方の系統が失敗してももう片方は
    # 必ず試みる。どちらか一方にでも残ればその取得単位は成功扱い（可用性最大化）。
    # Notion を正本とする原則は維持しつつ、ローカル API の可用性のため対称化する。
    # 失敗は隠さず notion_failed / mirror_failed に計上し warning に出す（§3-2）。
    # 両系統とも失敗した取得単位のみ呼び出し側が ctx.failed に数える。

    def _mirror(self, fn, label: str) -> bool | None:
        """ローカルへ1件ミラーする。

        返り値: 書けた=True / 失敗=False / ローカル系統なし(未設定・接続不可)=None。
        ミラー失敗で収集は止めない（mirror_failed に計上し warning §3-2）。
        """
        if self.local is None:
            return None
        try:
            fn(self.local)
            return True
        except Exception as exc:  # noqa: BLE001 - ミラー失敗で収集を止めない (§3-2)
            self.mirror_failed += 1
            logger.warning("ローカルミラー失敗 (%s): %s", label, exc)
            return False

    def _cloud(self, fn, label: str) -> bool | None:
        """Cloudflare へ1件書く。

        返り値: 書けた=True / 失敗=False / Cloudflare 系統なし(未設定・dry-run)=None。
        ローカルミラーと同じくベストエフォートで、失敗しても収集は止めない
        （cloud_failed に計上し warning §3-2）。移行期間中は Notion とローカルが
        並行して正本を持つため、片系統の失敗で取得単位を落とす必要がない。
        """
        if self.cloud is None:
            return None
        try:
            result = fn(self.cloud)
        except Exception as exc:  # noqa: BLE001 - Cloudflare 失敗で収集を止めない (§3-2)
            self.cloud_failed += 1
            logger.warning("Cloudflare 書き込み失敗 (%s): %s", label, exc)
            return False
        if result is False:
            self.cloud_failed += 1
        return result

    def _mirror_record(self, store: LocalStore, record, include_lifecycle: bool) -> None:
        """record の型に応じて適切なローカル upsert を呼ぶ（純粋なディスパッチ）。"""
        from ..models import (
            DisclosureRecord,
            FinancialSummaryRecord,
            PriceTechnicalRecord,
            RawArtifact,
            StockMasterRecord,
        )

        if isinstance(record, StockMasterRecord):
            store.upsert_stock_master(record, include_lifecycle=include_lifecycle)
        elif isinstance(record, PriceTechnicalRecord):
            store.upsert_price_technical(record)
        elif isinstance(record, FinancialSummaryRecord):
            store.upsert_financial_summary(record)
        elif isinstance(record, DisclosureRecord):
            store.upsert_disclosure(record)
        elif isinstance(record, RawArtifact):
            store.upsert_raw_artifact(record)
        else:
            raise TypeError(f"mirror 未対応の型: {type(record).__name__}")

    def mirror(self, record, *, include_lifecycle: bool = True) -> bool | None:
        """Notion へ upsert 済みの record を型に応じてローカルへもミラーする。"""
        return self._mirror(
            lambda store: self._mirror_record(store, record, include_lifecycle),
            type(record).__name__,
        )

    def mirror_mark_absent(self, code: str) -> bool | None:
        """コードリスト消失 (listed=False) をローカルへ反映。"""
        return self._mirror(lambda s: s.mark_master_absent(code), "mark_absent")

    def mirror_lifecycle(self, record) -> bool | None:
        """上場廃止/新規上場の状態反映をローカルへ。"""
        return self._mirror(lambda s: s.apply_disclosure_lifecycle(record), "lifecycle")

    def mirror_xbrl_facts(self, tidy, artifact) -> bool | None:
        """XBRL 全ファクト（定性 textBlock 含む）をローカル専用テーブル⑧へミラーする。

        Notion には対応オブジェクトが無い（③は財務サマリの要約のみ）ためローカル限定の
        派生ストア。dual-write ではなくベストエフォート（失敗は mirror_failed に計上し
        収集は止めない §3-2）。tidy は pandas DataFrame か dict 反復可能。None/空は no-op。
        """
        if self.local is None or tidy is None:
            return None
        rows = tidy.to_dict("records") if hasattr(tidy, "to_dict") else list(tidy)
        if not rows:
            return None
        return self._mirror(lambda s: s.upsert_xbrl_facts(rows, artifact), "xbrl_facts")

    def cloud_financial_summary(
        self, record, *, doc_id: str | None, raw_sha256: str | None
    ) -> bool | None:
        """③財務サマリを Cloudflare 正本 (D1 jss_financials) へ書く。

        Notion / ローカルとは独立のベストエフォート（`_cloud` が握って
        cloud_failed に計上する）。移行期間中は Notion とローカルが並行して
        正本を持つので、ここの失敗で取得単位を落とす必要はない (§3-2)。
        """
        if record is None:
            # ここに None が来るのは呼び出し側のガード漏れ。黙って no-op にすると
            # 「③ が入らないのに誰も気づかない」に戻るので分かるようにする。
            raise ValueError("cloud_financial_summary に None を渡している")
        return self._cloud(
            lambda c: c.upsert_financial_summary(
                record, doc_id=doc_id, raw_sha256=raw_sha256
            ),
            f"③{doc_id or record.code}",
        )

    def _persist(self, notion_write, local_write, label: str) -> bool:
        """Notion とローカルへ独立に書き、少なくとも一方に残せたかを返す。

        - notion_write(): Notion 書き込み（例外で失敗）。失敗しても握って
          notion_failed に計上し、ローカル書き込みは必ず試みる。
        - local_write(store): ローカル書き込み（_mirror が握って bool|None を返す）。
        返り値: True=少なくとも一方に永続化できた / False=両系統とも失敗。
        ローカル系統が無い(None)場合は Notion の成否がそのまま結果になる
        （＝従来の Notion 単独運用と同じ挙動を保つ）。
        """
        notion_ok = False
        try:
            notion_write()
            notion_ok = True
        except Exception as exc:  # noqa: BLE001 - 片系統失敗でも他系統へ書く
            self.notion_failed += 1
            logger.warning("Notion 書き込み失敗（%s。ローカルは試行）: %s", label, exc)
        local_ok = self._mirror(local_write, label)
        return notion_ok if local_ok is None else (notion_ok or local_ok)

    def persist(
        self, record, notion_write, *, label: str, include_lifecycle: bool = True
    ) -> bool:
        """record を Notion とローカルへ独立に永続化する（双方向フェールセーフ）。

        返り値が False（両系統とも失敗）のとき、呼び出し側は add_failure すること。
        """
        return self._persist(
            notion_write,
            lambda store: self._mirror_record(store, record, include_lifecycle),
            label,
        )

    def persist_mark_absent(self, code: str, notion_write, *, label: str) -> bool:
        """listed=False を Notion とローカルへ独立に反映する。"""
        return self._persist(notion_write, lambda s: s.mark_master_absent(code), label)

    def persist_lifecycle(self, record, notion_write, *, label: str) -> bool:
        """ライフサイクル状態 (上場廃止/新規上場) を Notion とローカルへ独立反映する。"""
        return self._persist(
            notion_write, lambda s: s.apply_disclosure_lifecycle(record), label
        )

    def upload_raw(self, artifact, *, sha_map=None, sha_map_date=None) -> str | None:
        """原本を Notion ⑤ とローカル ⑤ へ独立に保存する（双方向フェールセーフ）。

        Notion ⑤ の raw_page_id を返す（Notion 失敗時は None）。両系統とも原本を
        保存できなかった場合のみ RawUploadError を送出し、呼び出し側はその取得単位の
        構造化書き込みを中止する（原本ゼロ＝トレーサビリティ喪失 §3-3/§8.1-4）。
        どちらか一方にでも原本が残れば構造化書き込みを許可する。

        `sha_map` / `sha_map_date` は ⑤ 重複検索の事前マップ（L-21）。
        省略時は従来どおり原本ごとに検索する。
        """
        from ..notion import file_upload

        notion_err: Exception | None = None
        raw_page_id: str | None = None
        try:
            raw_page_id = file_upload.upload_raw_artifact(
                self.client, self.settings, artifact,
                sha_map=sha_map, sha_map_date=sha_map_date,
            )
        except Exception as exc:  # noqa: BLE001 - ローカル ⑤ への保存を試みるため一旦握る
            notion_err = exc
            self.notion_failed += 1
            logger.warning("Notion ⑤ 原本UL失敗（ローカル ⑤ を試行）: %s", exc)
        local_ok = self._mirror(lambda s: s.upsert_raw_artifact(artifact), "RawArtifact")
        # Cloudflare は移行中の第3系統。R2 に原本が残れば D1 索引が欠けても
        # トレーサビリティは保たれるので、ここでは取得単位を落とさない。
        self._cloud(lambda c: c.upsert_raw_artifact(artifact), f"⑤{artifact.filename}")
        if notion_err is not None and local_ok is not True:
            # Notion ⑤・ローカル ⑤ のいずれにも原本が残らなかった → 取得単位を中止
            raise file_upload.RawUploadError(
                "原本を Notion ⑤・ローカル ⑤ のいずれにも保存できず取得単位を中止: "
                f"{artifact.filename}"
            ) from notion_err
        return raw_page_id


def build_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--dry-run", action="store_true", help="Notion へ書き込まない (§3-6)")
    parser.add_argument(
        "--date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=None,
        help="対象日 (省略時はジョブ毎の既定)",
    )
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限 (段階的取り込み §1)")
    parser.add_argument("--codes", default=None, help="対象銘柄コードのカンマ区切り (テスト用)")
    parser.add_argument(
        "--db-target", choices=DB_TARGETS, default=DB_TARGET_CLOUD,
        help="ローカルDB接続プロファイル (cloud=既定/クラウド経由, lan=同一LAN)",
    )
    return parser


def github_run_url(env: dict[str, str]) -> str | None:
    server = env.get("GITHUB_SERVER_URL")
    repo = env.get("GITHUB_REPOSITORY")
    run_id = env.get("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def _status(ctx: JobContext, crashed: bool) -> str:
    if crashed:
        return STATUS_FAILURE
    if ctx.failed == 0:
        return STATUS_SUCCESS
    if ctx.processed > 0:
        return STATUS_PARTIAL
    return STATUS_FAILURE


def run_job(
    job_name: str,
    fn,
    argv: list[str] | None = None,
    *,
    parser: argparse.ArgumentParser | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """ジョブ実行のエントリポイント。fn(ctx) を実行し ⑦ へ記録する。

    返り値は終了コード (成功/一部失敗=0, 失敗=1)。
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = parser or build_parser(job_name)
    args = parser.parse_args(argv)
    resolved_env = dict(os.environ) if env is None else env
    settings = load_settings(dry_run=True if args.dry_run else None, env=resolved_env)

    if settings.dry_run:
        # DB ID 未設定でも dry-run は完走させる（書き込みは記録のみ §3-6）
        for key in DB_REGISTRY:
            settings.db_ids.setdefault(key, f"dry-run-db-{key}")

    client = NotionClient(
        settings.notion_token, rps=settings.notion_rps, dry_run=settings.dry_run
    )
    ctx = JobContext(settings=settings, client=client, args=args)
    # dual-write: 接続情報があればローカル PostgreSQL へミラー。dry-run は書き込まない。
    # --db-target で cloud(既定/クラウド経由) か lan(同一LAN) を選ぶ。
    if not settings.dry_run:
        target = getattr(args, "db_target", DB_TARGET_CLOUD)
        ctx.local = connect_local_store(settings.local_store, target)
        # Cloudflare 正本。資格情報が無ければ enabled=False で何もしない。
        if settings.cloud_store.enabled():
            from ..cloud_store.sink import CloudSink  # noqa: PLC0415 - 任意依存

            ctx.cloud = CloudSink(settings.cloud_store, writer=job_name)
            logger.info(
                "Cloudflare 正本へ書き込む (R2=%s / D1=%s)",
                settings.cloud_store.r2_enabled(), settings.cloud_store.d1_enabled(),
            )
    started = time.monotonic()
    crashed = False
    try:
        fn(ctx)
    except Exception:
        crashed = True
        logger.error("ジョブ異常終了: %s\n%s", job_name, traceback.format_exc())

    duration = time.monotonic() - started
    status = _status(ctx, crashed)
    # Notion ⑦ 収集ジョブログは廃止。実行履歴は D1 jss_job_runs に一本化した。

    # D1 への実行記録。器 (jss_job_runs) はあったが writer が存在せず 0 行のままで、
    # EDINET が 11 営業日連続 processed=0 で「成功」していたことを誰も検知
    # できなかった。ここが唯一の機械可読な実行履歴になる。
    if ctx.cloud is not None and ctx.cloud.settings.d1_enabled():
        from ..cloud_store.d1 import D1Store  # noqa: PLC0415 - 任意依存
        from ..cloud_store.ops import safe_record_job_run  # noqa: PLC0415

        safe_record_job_run(
            D1Store(ctx.cloud.settings, writer=job_name),
            job_name=job_name,
            status=status,
            processed=ctx.processed,
            failed=ctx.failed,
            failed_codes=ctx.failed_codes,
            run_url=github_run_url(resolved_env),
            duration_secs=round(duration, 1),
            finished_at=int(time.time()),
        )

    # ローカル ⑦ への記録 + 接続クローズ（失敗してもジョブ結果に影響させない）
    if ctx.local is not None:
        try:
            ctx.local.write_job_log(
                job_name, status, ctx.processed, ctx.failed, ctx.failed_codes[:50],
                run_url=github_run_url(resolved_env), duration_secs=round(duration, 1),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("⑦ ローカルジョブログ記録失敗: %s", exc)
        ctx.local.close()

    # 書き込み degrade サマリ（local 接続の有無に依らず出力。両カウンタ 0 なら無出力）。
    # Notion 単独運用でも notion_failed を俯瞰できるよう dual-write 前提の文言は避ける。
    if ctx.mirror_failed or ctx.notion_failed or ctx.cloud_failed:
        logger.warning(
            "書き込み degrade サマリ: Notion失敗 %d 件 / ローカルミラー失敗 %d 件"
            " / Cloudflare失敗 %d 件"
            "（片系統失敗は継続し、両系統とも失敗した分のみ failed に計上 §3-2）",
            ctx.notion_failed, ctx.mirror_failed, ctx.cloud_failed,
        )

    logger.info(
        "%s: %s (processed=%d failed=%d %.1fs)",
        job_name, status, ctx.processed, ctx.failed, duration,
    )
    return 1 if status == STATUS_FAILURE else 0


def parse_codes_arg(args: argparse.Namespace) -> list[str] | None:
    if args.codes:
        return [c.strip() for c in str(args.codes).split(",") if c.strip()]
    return None


def apply_limit(items: list, limit: int | None) -> list:
    return items[:limit] if limit else items


def main_exit(code: int) -> None:
    sys.exit(code)
