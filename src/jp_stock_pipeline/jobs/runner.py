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

from ..config import DB_REGISTRY, ConfigError, Settings, load_settings
from ..notion.client import NotionClient
from ..notion.upsert import write_job_log

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

    def add_success(self, n: int = 1) -> None:
        self.processed += n

    def add_failure(self, code: str, reason: str = "") -> None:
        """欠損・取得失敗は隠さず記録する (§3-2)。"""
        self.failed += 1
        self.failed_codes.append(code)
        logger.warning("失敗: %s %s", code, reason)


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
