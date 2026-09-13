"""半期報告書の表記と ③ の連結区分キーの移行（既定は dry-run）。

使い方（リポジトリ直下で。値は .env から読むが画面には出さない）:

    nix develop -c uv run python scripts/migrate_halfyear_labels.py            # 全対象を dry-run
    nix develop -c uv run python scripts/migrate_halfyear_labels.py --target options --apply
    nix develop -c uv run python scripts/migrate_halfyear_labels.py --target financials --apply

## 何を書き換えるか

- options: Notion ③「開示種別」に「中間」、④「書類種別」に「半期報告」の選択肢を足す。
  `python -m jp_stock_pipeline.notion.schema` は既存 DB に**不足プロパティしか**足さない
  ので、選択肢はここで足す。既存の選択肢は id・名前・色をそのまま送り直す（送らなかった
  選択肢が消える API の挙動を踏まないため）。書き込み時の自動作成には頼らない。
- financials: Notion ③ の「2Q」のうち、決算期末が 2024-06-30 以後の行を「中間」へ。
  タイトル末尾も書き手と同じ規則で書き直す。判定は書き手と同じ
  `transform/normalize.interim_disclosure_type`（規則が同じなので、移行した行と後から
  取り直した書き込みが同じキーに着地する）。
- disclosures: Notion ④ の「四半期報告」のうち、EDINET の半期報告書 (160) と
  訂正半期報告書 (170) の行を「半期報告」へ。④ には docTypeCode が残っていないので
  書類名で判定する（`(?<!四)半期報告書`）。ローカルの EDINET 書類一覧のうち secCode を
  持つ 140/150/160/170 の 22,331 件で、この判定と docTypeCode は完全に一致した。
  重複ページ（同じ書類管理番号の複数ページ）もすべて書き換えるので、重複の片付けを
  先に済ませても、DB を作り直した後でも同じように動く。DB ID は設定から読む。
- d1 / local: D1 `jss_financials` とローカル PG `financials` で ③ と同じ「2Q」→「中間」。
  D1 の接続情報 (CF_ACCOUNT_ID / CF_API_TOKEN / CF_D1_DATABASE_ID) が無ければ、
  wrangler で流す SQL を表示するだけにする。**D1 は書き手が旧行を採用しない**
  （PK に開示種別を含む INSERT ... ON CONFLICT）ので、Notion と違って順序に依存する。
  下の「衝突」の畳み込みを apply に含めるのはそのため。
- 連結区分 (③ のキー): Notion ③ は書き手が「連結単体が空」の行を採用するので
  書き換えは要らない。空の行を数えるだけにする。ローカル PG の PK 張り替えは
  LocalStore の接続時 DDL (`local_store/schema.FINANCIALS_PK_MIGRATION`) が行い、
  `--target local --apply` はそれを流してから UPDATE する。

## 衝突

同じ (銘柄コード, 決算期末, 連結単体) に「中間」の行が既にあれば、その「2Q」の行は
書き換えずに conflicts として出す（書き換えると同じキーの行が 2 つになる）。

- Notion ③: 新しいコードの書き手は「2Q」の旧行を採用するので、移行より先にラベル変更が
  出ても衝突は起きにくい。出たときは中身を比べて、不要な方を Notion で archive する。
- D1 / ローカル PG: 書き手は旧行を採用しない。マージ後・移行前に同じ期を書くと
  「2Q」（旧コード）と「中間」（新コード）の 2 行になる。2026-09-13 時点の D1 の対象
  31 行は決算期末がすべて 2026-07-31 で、TDnet の短信が「2Q」で入り、同じ期の
  半期報告書（提出期限 2026-09-14）が EDINET から「中間」で来る組み合わせそのもの。
  そこで apply は、衝突した「2Q」行を「中間」行へ**畳み込んでから**消し、残りを
  書き換える（fold → drop_folded → apply。どれも流し直して結果が変わらない）。
  畳み込みは書き手の upsert と同じ規則: 開示日時の新しい側の値を優先し、NULL は
  もう一方で埋める (COALESCE)、来歴列は新しい側、license_tag は厳しい側。
  採らなかった案: (a) 衝突行を残して人が消す — 短信の予想値など片方にしか無い値が
  消える / 手作業が要る。(b) D1 の書き手に旧行の採用を足す — 書き込み 1 件ごとに
  文が増え、並行セッションが触る cloud_store/sink.py の経路に手が入る。

## 所要時間の目安（Notion 2.5 req/s。2026-09-13 監査の件数）

- financials: 読み取り 約 30 リクエスト + 更新 8,315 件 ≒ 56 分
- disclosures: 読み取り 約 120 リクエスト + 更新 8,573 件 ≒ 58 分
- options: 4 リクエスト / d1・local: 数秒

途中で止まっても、流し直せば残りだけを書き換える（冪等）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from jp_stock_pipeline.cloud_store import financials as d1_financials
from jp_stock_pipeline.config import Settings, load_settings
from jp_stock_pipeline.licensing import LicenseTag, stricter_tag_sql
from jp_stock_pipeline.local_store import mappers as local_mappers
from jp_stock_pipeline.models import FinancialSummaryRecord, Provenance, Source
from jp_stock_pipeline.notion import schema as S
from jp_stock_pipeline.notion.client import NotionClient, QueryTruncatedError
from jp_stock_pipeline.notion.upsert import financial_summary_title, select_prop, title_prop
from jp_stock_pipeline.transform.normalize import (
    DISCLOSURE_TYPE_INTERIM,
    DISCLOSURE_TYPE_SECOND_QUARTER,
    INTERIM_FIRST_PERIOD_END,
    interim_disclosure_type,
)

logger = logging.getLogger("migrate_halfyear_labels")

TARGETS: tuple[str, ...] = ("options", "financials", "disclosures", "d1", "local")

DOC_TYPE_QUARTERLY = "四半期報告"
DOC_TYPE_HALFYEAR = "半期報告"
HALFYEAR_TITLE_RE = re.compile(r"(?<!四)半期報告書")

# 足す選択肢: (論理 DB キー, プロパティ名, 選択肢名)
WANTED_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("financials", S.FIN_PROP_DISCLOSURE_TYPE, DISCLOSURE_TYPE_INTERIM),
    ("disclosures", S.DISC_PROP_DOC_TYPE, DOC_TYPE_HALFYEAR),
)

# 読み取りの窓。③ は決算期末、④ は開示日時で区切る。1 クエリ 10,000 件で打ち切られる
# ので、打ち切られた窓は半分に割って読み直す。
FIN_SCAN_END = date(2032, 1, 1)
DISC_SCAN_START = date(2015, 1, 1)
DISC_SCAN_END = date(2032, 1, 1)

D1_TABLE = "jss_financials"
LOCAL_TABLE = "financials"


class MigrationError(RuntimeError):
    pass


@dataclass
class Update:
    page_id: str
    properties: dict
    detail: dict


@dataclass
class Plan:
    target: str
    updates: list[Update] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Notion のページから値を読む
# ---------------------------------------------------------------------------


def prop_text(page: dict, name: str) -> str | None:
    value = (page.get("properties") or {}).get(name) or {}
    kind = value.get("type")
    if kind is None:  # テストダブルや書き込み用 payload は type を持たない
        kind = next((k for k in ("select", "date", "title", "rich_text") if k in value), None)
    if kind in ("rich_text", "title"):
        parts = value.get(kind) or []
        text = "".join(p.get("plain_text") or (p.get("text") or {}).get("content", "") for p in parts)
        return text or None
    if kind == "date":
        return (value.get("date") or {}).get("start")
    if kind == "select":
        return (value.get("select") or {}).get("name")
    return None


def _period_end(page: dict) -> date | None:
    text = prop_text(page, S.FIN_PROP_PERIOD_END)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _fin_key(page: dict) -> tuple[str | None, date | None, str | None]:
    return (
        prop_text(page, S.FIN_PROP_CODE),
        _period_end(page),
        prop_text(page, S.FIN_PROP_CONSOLIDATED),
    )


# ---------------------------------------------------------------------------
# 計画（純粋関数。テスト対象）
# ---------------------------------------------------------------------------


def interim_title(code: str, fiscal_period_end: date) -> str:
    """書き手 (notion/upsert.financial_summary_title) と同じタイトル。"""
    return financial_summary_title(
        SimpleNamespace(
            code=code, fiscal_period_end=fiscal_period_end,
            disclosure_type=DISCLOSURE_TYPE_INTERIM,
        )
    )


def plan_financials(second_quarter_pages: Iterable[dict], interim_pages: Iterable[dict]) -> Plan:
    """③ の「2Q」→「中間」の計画。"""
    plan = Plan("financials")
    interim_by_key: dict[tuple, list[str]] = defaultdict(list)
    for page in interim_pages:
        interim_by_key[_fin_key(page)].append(page["id"])
    by_source: Counter = Counter()
    keys: Counter = Counter()
    scanned = not_target = 0
    for page in second_quarter_pages:
        scanned += 1
        code, period_end, consolidated = _fin_key(page)
        dtype = prop_text(page, S.FIN_PROP_DISCLOSURE_TYPE)
        if (
            dtype != DISCLOSURE_TYPE_SECOND_QUARTER or not code or period_end is None
            or interim_disclosure_type(dtype, period_end) != DISCLOSURE_TYPE_INTERIM
        ):
            not_target += 1
            continue
        key = (code, period_end, consolidated)
        detail = {
            "code": code, "fiscal_period_end": period_end.isoformat(),
            "consolidated": consolidated, "source": prop_text(page, S.PROP_SOURCE),
        }
        if key in interim_by_key:
            plan.conflicts.append(
                {**detail, "second_quarter_page": page["id"],
                 "interim_pages": interim_by_key[key]}
            )
            continue
        keys[key] += 1
        by_source[detail["source"] or "(空)"] += 1
        plan.updates.append(
            Update(
                page_id=page["id"],
                properties={
                    S.FIN_PROP_DISCLOSURE_TYPE: select_prop(DISCLOSURE_TYPE_INTERIM),
                    S.FIN_PROP_TITLE: title_prop(interim_title(code, period_end)),
                },
                detail=detail,
            )
        )
    plan.counts = {
        "scanned": scanned,
        "not_target": not_target,
        "updates": len(plan.updates),
        "updates_by_source": dict(sorted(by_source.items())),
        "conflicts": len(plan.conflicts),
        # 同じキーの「2Q」が既に複数ある (#13 の重複)。すべて「中間」へ移すので、
        # 重複はそのまま引き継がれる（書き手は最古を正とする）。
        "duplicate_keys": sum(1 for n in keys.values() if n > 1),
    }
    return plan


def plan_disclosures(pages: Iterable[dict]) -> Plan:
    """④ の「四半期報告」→「半期報告」の計画（EDINET の半期報告書・訂正半期報告書だけ）。"""
    plan = Plan("disclosures")
    doc_ids: Counter = Counter()
    scanned = not_target = 0
    for page in pages:
        scanned += 1
        title = prop_text(page, S.DISC_PROP_TITLE) or ""
        if (
            prop_text(page, S.DISC_PROP_DOC_TYPE) != DOC_TYPE_QUARTERLY
            or prop_text(page, S.PROP_SOURCE) != Source.EDINET.value
            or not HALFYEAR_TITLE_RE.search(title)
        ):
            not_target += 1
            continue
        doc_id = prop_text(page, S.DISC_PROP_DOC_ID)
        doc_ids[doc_id] += 1
        plan.updates.append(
            Update(
                page_id=page["id"],
                properties={S.DISC_PROP_DOC_TYPE: select_prop(DOC_TYPE_HALFYEAR)},
                detail={"doc_id": doc_id},
            )
        )
    plan.counts = {
        "scanned": scanned,
        "not_target": not_target,
        "updates": len(plan.updates),
        "duplicate_doc_ids": sum(1 for n in doc_ids.values() if n > 1),
        "duplicate_extra_pages": sum(n - 1 for n in doc_ids.values() if n > 1),
    }
    return plan


def select_options_payload(db_info: dict, prop: str, wanted: Sequence[str]) -> dict | None:
    """足りない選択肢を足す update_database の properties。足りていれば None。

    既存の選択肢は id・名前・色をそのまま含める。Notion の select の options を
    更新するとき、送らなかった既存の選択肢が消える扱いを避けるため。
    """
    prop_info = (db_info.get("properties") or {}).get(prop)
    if not prop_info or prop_info.get("type", "select") != "select":
        raise MigrationError(f"select プロパティが見つからない: {prop}")
    existing = (prop_info.get("select") or {}).get("options") or []
    names = {o.get("name") for o in existing}
    missing = [name for name in wanted if name not in names]
    if not missing:
        return None
    keep = []
    for option in existing:
        kept = {k: option[k] for k in ("id", "name", "color") if option.get(k)}
        keep.append(kept)
    return {prop: {"select": {"options": keep + [{"name": name} for name in missing]}}}


def option_names(db_info: dict, prop: str) -> list[str]:
    prop_info = (db_info.get("properties") or {}).get(prop) or {}
    return [o.get("name") for o in (prop_info.get("select") or {}).get("options") or []]


def _fold_columns(table: str) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """(COALESCE でマージする列, 新しい側で上書きする列, license 列)。書き手の分類をそのまま使う。"""
    if table == D1_TABLE:
        return (
            d1_financials.MERGE_COLUMNS, d1_financials.OVERWRITE_COLUMNS,
            d1_financials.LICENSE_COLUMN,
        )
    # ローカルは列の一覧を定数で持たないので、書き手の params から取る（値は使わない）。
    _, params = local_mappers.financial_upsert(
        FinancialSummaryRecord(
            code="0000", fiscal_period_end=date(2000, 1, 1), disclosure_type="本決算",
            provenance=Provenance(
                source=Source.EDINET, license_tag=LicenseTag.COMMERCIAL_OK,
                data_date=None, fetched_at=datetime(2000, 1, 1, tzinfo=UTC),
            ),
        )
    )
    fixed = {
        *local_mappers.FIN_PK, *local_mappers.FIN_OVERWRITE_COLUMNS,
        local_mappers.FIN_LICENSE_COLUMN,
    }
    return (
        tuple(c for c in params if c not in fixed), local_mappers.FIN_OVERWRITE_COLUMNS,
        local_mappers.FIN_LICENSE_COLUMN,
    )


def interim_sql(table: str) -> dict[str, str]:
    """D1 / ローカル PG 共通の SQL。値はすべて定数から作り、外部入力を埋め込まない。

    `IS NOT DISTINCT FROM` は SQLite (>= 3.39) と PostgreSQL で同じ意味を持つ。
    ローカル PG は PK 張り替え前だと連結区分に NULL がありうるので、= ではなくこれを使う。
    `UPDATE ... FROM`（自己結合）は SQLite (>= 3.33) と PostgreSQL の両方にある。

    apply は fold → drop_folded → apply の順に流す（docstring「衝突」）。
    """
    if table not in (D1_TABLE, LOCAL_TABLE):
        raise MigrationError(f"未知の表: {table}")
    q2 = DISCLOSURE_TYPE_SECOND_QUARTER
    interim = DISCLOSURE_TYPE_INTERIM
    first = INTERIM_FIRST_PERIOD_END.isoformat()

    def target(alias: str) -> str:
        return f"{alias}.disclosure_type = '{q2}' AND {alias}.fiscal_period_end >= '{first}'"

    def same_key_interim(outer: str) -> str:
        return (
            f"SELECT 1 FROM {table} b WHERE b.code = {outer}.code"
            f" AND b.fiscal_period_end = {outer}.fiscal_period_end"
            f" AND b.consolidated IS NOT DISTINCT FROM {outer}.consolidated"
            f" AND b.disclosure_type = '{interim}'"
        )

    merge_cols, overwrite_cols, license_col = _fold_columns(table)
    # q = 畳み込まれる「2Q」行。q の方が新しい開示なら q の値を優先する（書き手の
    # disclosed_at ガードと同じ向き。既存側が NULL なら新しい側とみなす）。
    q_newer = (
        f"(q.disclosed_at > {table}.disclosed_at"
        f" OR ({table}.disclosed_at IS NULL AND q.disclosed_at IS NOT NULL))"
    )
    assignments = [
        f"{c} = CASE WHEN {q_newer} THEN COALESCE(q.{c}, {table}.{c})"
        f" ELSE COALESCE({table}.{c}, q.{c}) END"
        for c in merge_cols
    ]
    assignments += [
        f"{c} = CASE WHEN {q_newer} THEN q.{c} ELSE {table}.{c} END" for c in overwrite_cols
    ]
    assignments.append(
        f"{license_col} = " + stricter_tag_sql(f"q.{license_col}", f"{table}.{license_col}")
    )
    if table == LOCAL_TABLE:
        assignments.append("updated_at = CURRENT_TIMESTAMP")
    return {
        "count": (
            f"SELECT a.source, COUNT(*) AS n FROM {table} a WHERE {target('a')}"
            " GROUP BY a.source ORDER BY a.source"
        ),
        "fold": (
            f"UPDATE {table} SET {', '.join(assignments)} FROM {table} AS q"
            f" WHERE {table}.disclosure_type = '{interim}' AND {target('q')}"
            f" AND q.code = {table}.code AND q.fiscal_period_end = {table}.fiscal_period_end"
            f" AND q.consolidated IS NOT DISTINCT FROM {table}.consolidated"
        ),
        "drop_folded": (
            f"DELETE FROM {table} WHERE {target(table)} AND EXISTS ({same_key_interim(table)})"
        ),
        "conflicts": (
            f"SELECT COUNT(*) AS n FROM {table} a WHERE {target('a')}"
            f" AND EXISTS ({same_key_interim('a')})"
        ),
        "apply": (
            f"UPDATE {table} SET disclosure_type = '{interim}' WHERE {target(table)}"
            f" AND NOT EXISTS ({same_key_interim(table)})"
        ),
    }


# ---------------------------------------------------------------------------
# Notion の読み書き
# ---------------------------------------------------------------------------


def scan_windows(
    client: NotionClient, db_id: str, base: list[dict], date_prop: str,
    windows: Iterable[tuple[date, date]],
) -> dict[str, dict]:
    pages: dict[str, dict] = {}
    for start, end in windows:
        _scan(client, db_id, base, date_prop, start, end, pages)
    return pages


def _scan(client, db_id, base, date_prop, start, end, out) -> None:
    flt = {"and": [
        *base,
        {"property": date_prop, "date": {"on_or_after": start.isoformat()}},
        {"property": date_prop, "date": {"before": end.isoformat()}},
    ]}
    try:
        for page in client.query_database(db_id, filter=flt, strict=True):
            out[page["id"]] = page
    except QueryTruncatedError:
        days = (end - start).days
        if days <= 1:
            raise MigrationError(
                f"{date_prop} {start} の 1 日で 10,000 件を超え、分割して読めない"
            ) from None
        mid = start + timedelta(days=days // 2)
        _scan(client, db_id, base, date_prop, start, mid, out)
        _scan(client, db_id, base, date_prop, mid, end, out)


def year_windows(first: date, last: date) -> list[tuple[date, date]]:
    windows = []
    start = first
    while start < last:
        end = min(date(start.year + 1, 1, 1), last)
        windows.append((start, end))
        start = end
    return windows


def apply_updates(client: NotionClient, plan: Plan, limit: int | None, rps: float) -> dict:
    todo = plan.updates if limit is None else plan.updates[:limit]
    started = time.monotonic()
    done = failed = 0
    failed_pages: list[str] = []
    logger.info("%s: %d 件を書き換える（目安 %.0f 分）", plan.target, len(todo), len(todo) / rps / 60)
    for i, update in enumerate(todo, 1):
        try:
            client.update_page(update.page_id, update.properties)
            done += 1
        except Exception as exc:  # noqa: BLE001 - 1 件の失敗で止めず、最後に数えて返す
            failed += 1
            failed_pages.append(update.page_id)
            logger.error("%s: 書き換え失敗 page=%s: %s", plan.target, update.page_id, exc)
        if i % 500 == 0:
            logger.info("%s: %d/%d 件 (%.0f 秒)", plan.target, i, len(todo), time.monotonic() - started)
    return {"applied": done, "failed": failed, "failed_pages": failed_pages[:50],
            "elapsed_sec": round(time.monotonic() - started)}


def run_options(client: NotionClient, settings: Settings, apply: bool) -> dict:
    report: dict = {}
    wanted: dict[tuple[str, str], list[str]] = defaultdict(list)
    for db_key, prop, name in WANTED_OPTIONS:
        wanted[(db_key, prop)].append(name)
    for (db_key, prop), names in wanted.items():
        db_id = settings.db_id(db_key)
        info = client.retrieve_database(db_id)
        before = option_names(info, prop)
        payload = select_options_payload(info, prop, names)
        entry = {"before": before, "missing": [n for n in names if n not in before]}
        if payload is not None and apply:
            client.update_database(db_id, properties=payload)
            after = option_names(client.retrieve_database(db_id), prop)
            lost = [n for n in before if n not in after]
            if lost or any(n not in after for n in names):
                raise MigrationError(f"選択肢の追加を確認できない: {db_key}.{prop} lost={lost} after={after}")
            entry["after"] = after
        report[f"{db_key}.{prop}"] = entry
    return report


def run_financials(client: NotionClient, settings: Settings, apply: bool, limit: int | None) -> dict:
    db_id = settings.db_id("financials")
    windows = year_windows(INTERIM_FIRST_PERIOD_END, FIN_SCAN_END)
    second_quarter = scan_windows(
        client, db_id,
        [{"property": S.FIN_PROP_DISCLOSURE_TYPE, "select": {"equals": DISCLOSURE_TYPE_SECOND_QUARTER}}],
        S.FIN_PROP_PERIOD_END, windows,
    )
    # Notion は存在しない select の選択肢でフィルタすると 400 を返す（2026-09-13 に本番の
    # dry-run で実測: 'select option "中間" not found'）。選択肢がまだ無ければ「中間」の行も
    # 無いので、検索せず衝突 0 として扱う。
    interim_exists = DISCLOSURE_TYPE_INTERIM in option_names(
        client.retrieve_database(db_id), S.FIN_PROP_DISCLOSURE_TYPE
    )
    interim = scan_windows(
        client, db_id,
        [{"property": S.FIN_PROP_DISCLOSURE_TYPE, "select": {"equals": DISCLOSURE_TYPE_INTERIM}}],
        S.FIN_PROP_PERIOD_END, windows,
    ) if interim_exists else {}
    plan = plan_financials(second_quarter.values(), interim.values())
    report: dict = {"counts": plan.counts, "conflicts": plan.conflicts[:50],
                    "interim_option_exists": interim_exists}
    if apply and not interim_exists:
        raise MigrationError(
            "③「開示種別」に選択肢「中間」が無い。先に --target options --apply を流す"
        )
    # 連結区分のキー (B): 書き手が採用するので書き換えない。数えるだけ。
    try:
        empty = client.query_database(
            db_id, filter={"property": S.FIN_PROP_CONSOLIDATED, "select": {"is_empty": True}},
            strict=True,
        )
        report["empty_consolidated_rows"] = len(empty)
    except QueryTruncatedError:
        report["empty_consolidated_rows"] = ">= 10000"
    report["estimated_minutes"] = round(len(plan.updates) / settings.notion_rps / 60, 1)
    if apply:
        report["result"] = apply_updates(client, plan, limit, settings.notion_rps)
    return report


def run_disclosures(client: NotionClient, settings: Settings, apply: bool, limit: int | None) -> dict:
    db_id = settings.db_id("disclosures")
    base = [
        {"property": S.DISC_PROP_DOC_TYPE, "select": {"equals": DOC_TYPE_QUARTERLY}},
        {"property": S.PROP_SOURCE, "select": {"equals": Source.EDINET.value}},
        # Notion 側で候補を絞る（最終判定は plan_disclosures の正規表現）。
        {"property": S.DISC_PROP_TITLE, "title": {"contains": "半期報告書"}},
        {"property": S.DISC_PROP_TITLE, "title": {"does_not_contain": "四半期報告書"}},
    ]
    pages = scan_windows(
        client, db_id, base, S.DISC_PROP_DISCLOSED_AT, year_windows(DISC_SCAN_START, DISC_SCAN_END)
    )
    plan = plan_disclosures(pages.values())
    if apply and DOC_TYPE_HALFYEAR not in option_names(
        client.retrieve_database(db_id), S.DISC_PROP_DOC_TYPE
    ):
        raise MigrationError("④「書類種別」に選択肢「半期報告」が無い。先に --target options --apply を流す")
    report: dict = {"counts": plan.counts,
                    "estimated_minutes": round(len(plan.updates) / settings.notion_rps / 60, 1)}
    if apply:
        report["result"] = apply_updates(client, plan, limit, settings.notion_rps)
    return report


# ---------------------------------------------------------------------------
# D1 / ローカル PG
# ---------------------------------------------------------------------------


def run_d1(settings: Settings, apply: bool) -> dict:
    sql = interim_sql(D1_TABLE)
    if not settings.cloud_store.d1_enabled():
        return {
            "configured": False,
            "note": "D1 の接続情報が無いので SQL だけを出す。kabulab-cf で "
                    "`npx wrangler d1 execute <DB> --remote --command \"<SQL>\"` の形で、"
                    "count と conflicts を見てから fold → drop_folded → apply の順に 1 文ずつ流す"
                    "（conflicts が 0 なら fold と drop_folded は 0 行で終わる）",
            "sql": sql,
        }
    from jp_stock_pipeline.cloud_store.d1 import D1Store

    store = D1Store(settings.cloud_store, writer="migrate_halfyear_labels")
    report: dict = {
        "configured": True,
        "count": store.query(sql["count"]),
        "conflicts": store.query(sql["conflicts"]),
    }
    if apply:
        # どれも条件付きで、再送しても結果が変わらない。途中で落ちても流し直せばよい。
        for step in ("fold", "drop_folded", "apply"):
            store.query(sql[step])
        report["after_count"] = store.query(sql["count"])
        report["after_conflicts"] = store.query(sql["conflicts"])
    return report


def run_local(settings: Settings, apply: bool) -> dict:
    if not settings.local_store.enabled():
        return {"configured": False, "note": "LOCAL_DB_HOST が無い（本番は未構成）"}
    import psycopg

    from jp_stock_pipeline.local_store.sink import LocalStore

    sql = interim_sql(LOCAL_TABLE)
    with psycopg.connect(**settings.local_store.connect_kwargs(), autocommit=True) as conn:
        def rows(statement: str) -> list[tuple]:
            with conn.cursor() as cur:
                cur.execute(statement)
                return cur.fetchall()

        report: dict = {"configured": True, "count": rows(sql["count"]),
                        "conflicts": rows(sql["conflicts"])}
        if apply:
            LocalStore(conn).init_schema()  # PK 張り替え（済んでいれば何もしない）
            with conn.transaction(), conn.cursor() as cur:
                for step in ("fold", "drop_folded", "apply"):
                    cur.execute(sql[step])
                    report[f"{step}_rows"] = cur.rowcount
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip().removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--target", action="append", choices=TARGETS,
                        help="対象（複数指定可。既定は全部）")
    parser.add_argument("--apply", action="store_true", help="書き込む（既定は dry-run）")
    parser.add_argument("--limit", type=int, default=None,
                        help="Notion の書き換えを先頭 N 件で止める（試し流し用）")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--out", type=Path, default=None, help="結果の JSON を保存する")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    targets = [t for t in TARGETS if t in (args.target or TARGETS)]
    env = {**read_env_file(args.env_file), **os.environ}
    settings = load_settings(dry_run=False, env=env)
    report: dict = {"apply": args.apply, "targets": targets}
    client = None
    if any(t in ("options", "financials", "disclosures") for t in targets):
        if not settings.notion_token:
            raise MigrationError("NOTION_TOKEN が無い")
        # dry-run でも読み取りは本物が要る。書き込みは dry_run=True の client が記録するだけ
        # にして、万一の呼び出しも本番へ届かないようにする。
        client = NotionClient(settings.notion_token, rps=settings.notion_rps,
                              dry_run=not args.apply)
    failed = False
    for target in targets:
        logger.info("== %s (%s)", target, "apply" if args.apply else "dry-run")
        if target == "options":
            report[target] = run_options(client, settings, args.apply)
        elif target == "financials":
            report[target] = run_financials(client, settings, args.apply, args.limit)
        elif target == "disclosures":
            report[target] = run_disclosures(client, settings, args.apply, args.limit)
        elif target == "d1":
            report[target] = run_d1(settings, args.apply)
        elif target == "local":
            report[target] = run_local(settings, args.apply)
        failed = failed or bool((report[target].get("result") or {}).get("failed"))
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
