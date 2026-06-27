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

from ..config import DB_REGISTRY, ConfigError, Settings, load_settings
from ..local_store import connect_local_store
from ..notion.client import NotionClient
from ..notion.upsert import write_job_log

if TYPE_CHECKING:
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
    mirror_failed: int = 0  # ローカルミラー失敗数 (ジョブ成否には影響させない)

    def add_success(self, n: int = 1) -> None:
        self.processed += n

    def add_failure(self, code: str, reason: str = "") -> None:
        """欠損・取得失敗は隠さず記録する (§3-2)。"""
        self.failed += 1
        self.failed_codes.append(code)
        logger.warning("失敗: %s %s", code, reason)

    # --- ローカル PostgreSQL への dual-write ミラー -------------------------
    # Notion を正本とし、ミラー失敗はジョブを止めない（可用性ベストエフォート）。
    # 失敗は mirror_failed に数え warning に出すが ctx.failed には足さない。

    def _mirror(self, fn, label: str) -> None:
        if self.local is None:
            return
        try:
            fn(self.local)
        except Exception as exc:  # noqa: BLE001 - ミラー失敗で収集を止めない (§3-2)
            self.mirror_failed += 1
            logger.warning("ローカルミラー失敗 (%s): %s", label, exc)

    def mirror(self, record, *, include_lifecycle: bool = True) -> None:
        """Notion へ upsert 済みの record を型に応じてローカルへもミラーする。"""
        from ..models import (
            DisclosureRecord,
            FinancialSummaryRecord,
            PriceTechnicalRecord,
            RawArtifact,
            StockMasterRecord,
        )

        def _do(store: LocalStore) -> None:
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

        self._mirror(_do, type(record).__name__)

    def mirror_mark_absent(self, code: str) -> None:
        """コードリスト消失 (listed=False) をローカルへ反映。"""
        self._mirror(lambda s: s.mark_master_absent(code), "mark_absent")

    def mirror_lifecycle(self, record) -> None:
        """上場廃止/新規上場の状態反映をローカルへ。"""
        self._mirror(lambda s: s.apply_disclosure_lifecycle(record), "lifecycle")

    def upload_raw(self, artifact) -> str | None:
        """原本を Notion ⑤ へアップロードし、同じ artifact をローカル ⑤ へもミラーする。

        Notion ⑤ の raw_page_id を返す（呼び出し側が relation に使う）。ミラーは
        notion_page_id がセットされた後に行うため raw_files.notion_page_id も埋まる
        （ミラー失敗は degrade。Notion ⑤ への保存自体は従来どおり）。
        """
        from ..notion import file_upload

        raw_page_id = file_upload.upload_raw_artifact(self.client, self.settings, artifact)
        self.mirror(artifact)
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
    if not settings.dry_run:
        ctx.local = connect_local_store(settings.local_store)
    started = time.monotonic()
    crashed = False
    try:
        fn(ctx)
    except Exception:
        crashed = True
        logger.error("ジョブ異常終了: %s\n%s", job_name, traceback.format_exc())

    duration = time.monotonic() - started
    status = _status(ctx, crashed)
    try:
        write_job_log(
            client,
            settings,
            job_name,
            status,
            ctx.processed,
            ctx.failed,
            ctx.failed_codes[:50],  # rich_text 上限対策。全量はジョブ標準出力に出る
            run_url=github_run_url(resolved_env),
            duration_secs=round(duration, 1),
        )
    except ConfigError as exc:
        logger.warning("⑦ ジョブログ未記録 (DB ID 未設定): %s", exc)
    except Exception as exc:
        logger.error("⑦ ジョブログ記録失敗: %s", exc)

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
        if ctx.mirror_failed:
            logger.warning(
                "ローカルミラー失敗 %d 件（Notion は正本として正常。詳細は上記ログ）",
                ctx.mirror_failed,
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
