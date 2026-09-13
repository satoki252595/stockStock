"""Notion ④開示書類の既存重複を、書類管理番号ごとに 1 ページへ集約する運用ツール (#13 の後始末)。

背景: ④ には同じ書類管理番号のページが複数ある（2026-09-13 監査で 423 docID・余分 1,312 行）。
95% は事前マップの窓が UTC だった不具合（PR #46 で修正済み）による実行ごとの増殖。
ユーザの決定（2026-09-13）で、DB を作り直さず今の DB を掃除する。先に全件をバックアップする。

3 段階:
1. バックアップ（既定の実行で必ず最初に行う。読み取りのみ）
   ④ の全ページを、relation を全件にしたページオブジェクト（properties の生 JSON・id・
   created_time・last_edited_time を含む）として gzip JSONL に書き、同じ場所の README.md に
   何を・いつ・なぜ・件数・sha256・戻し方を追記する。
2. 計画（既定の動作。読み取りのみ）
   書類管理番号ごとに 2 ページ以上あるグループについて
   - 正のページ = PR #46 の順序キーで最古（`upsert.oldest_page` をそのまま使う。
     書き直すと本番の書き手と正の選び方がずれうるので、自前で実装しない）
   - 書き戻す値 = 最後に編集されたコピーの書き込み可能なプロパティ
   - 関連付け（銘柄マスタ・原本）= 全コピーの和集合
   - 正のページが既に同じ値なら更新しない。アーカイブ対象 = 正のページ以外
   を JSON に書き出す。
3. 適用（`--apply --plan <計画>`。レビュー後に人が実行する）
   グループごとに書類管理番号で読み直し、計画後に編集・追加されたグループは飛ばして報告し、
   正のページを更新して読み直しで確かめてから、他のコピーだけを archive（Notion のゴミ箱。
   復元可能）する。途中で止まっても、再実行で続きから収束する。

使い方（nix develop 内で）:
    uv run python scripts/dedupe_notion_disclosures.py \\
        --backup-dir ~/stockStock-backup-20260913/notion-disclosures --plan-out plan.json
    uv run python scripts/dedupe_notion_disclosures.py --from-backup <jsonl.gz> --plan-out plan.json
    uv run python scripts/dedupe_notion_disclosures.py --apply --plan plan.json

採らなかった案:
- 作り直し（新 DB へ移す）: ①⑤の双方向 relation と既存ビュー・外部リンクが切れる。ユーザ判断で不採用。
- 正のページ = 最後に編集されたコピー: 本番の書き手（PR #46）は最古を更新するので、
  集約後に書き手と正が食い違い、次の実行でまた値が割れる。値だけを最新から持ってくる。
- 開示日時で範囲を区切る走査（監査スクリプトの方式）: 開示日時が空・範囲外のページを
  別クエリで拾う必要があり、取りこぼしの余地が残る。created_time は全ページにあり、
  1 分に 10,000 件は作れない（2.5 req/s）ので、1 分まで割れば必ず全件読める。
- 適用時に計画ファイルの値だけを信じて書く: 計画後に本番の書き手が正のページを更新すると
  新しい値を古い値で潰す。last_edited_time を必ず照合する。
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import logging
import os
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jp_stock_pipeline.notion import schema as S  # noqa: E402
from jp_stock_pipeline.notion import upsert  # noqa: E402
from jp_stock_pipeline.notion.client import NotionClient, QueryTruncatedError  # noqa: E402

logger = logging.getLogger("dedupe_notion_disclosures")

PLAN_VERSION = 1
DEFAULT_RPS = 2.5
# 関連付けを和集合にするプロパティ（④ → ① / ⑤）
RELATION_PROPS = (S.PROP_MASTER_RELATION, S.PROP_RAW_RELATION)
# 書き込めないプロパティ型。値は Notion が計算・管理する。
READ_ONLY_TYPES = frozenset({
    "formula", "rollup", "created_time", "last_edited_time", "created_by",
    "last_edited_by", "unique_id", "verification", "button",
})
# created_time で走査するときの最小の窓。1 分に 10,000 件は作れないので、ここまで割れば必ず読み切れる
MIN_WINDOW = timedelta(minutes=1)
INITIAL_WINDOW = timedelta(days=7)


class PlanError(RuntimeError):
    """計画ファイルが不正で、適用してはならない。"""


class UnsupportedPropertyError(RuntimeError):
    """書き戻し方を決めていない型のプロパティがある（黙って捨てずに止める）。"""


# ---------------------------------------------------------------------------
# 読み取り: 全件走査と relation の全件化
# ---------------------------------------------------------------------------


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _created_window_filter(start: datetime, end: datetime) -> dict:
    return {"and": [
        {"timestamp": "created_time", "created_time": {"on_or_after": _iso(start)}},
        {"timestamp": "created_time", "created_time": {"before": _iso(end)}},
    ]}


def scan_all_pages(
    client: NotionClient, db_id: str, *, floor: datetime, ceiling: datetime,
    window: timedelta = INITIAL_WINDOW,
) -> dict[str, dict]:
    """created_time の範囲で区切って全ページを読む。打ち切られたら窓を半分に割って読み直す。"""
    out: dict[str, dict] = {}

    def scan(start: datetime, end: datetime) -> None:
        try:
            pages = client.query_database(
                db_id, filter=_created_window_filter(start, end), strict=True,
            )
        except QueryTruncatedError:
            if end - start <= MIN_WINDOW:
                raise RuntimeError(
                    f"{_iso(start)} からの 1 分で 10,000 件を超えた。全件を読めないので止める"
                ) from None
            mid = start + (end - start) / 2
            scan(start, mid)
            scan(mid, end)
            return
        for page in pages:
            out[page["id"]] = page

    cursor = floor
    while cursor < ceiling:
        nxt = min(cursor + window, ceiling)
        scan(cursor, nxt)
        cursor = nxt
    return out


def fill_relations(client: NotionClient, page: dict) -> int:
    """relation の has_more が立っていれば、プロパティ取得 API で全件に置き換える。

    ページオブジェクトは relation を 25 件までしか載せない。戻り値は読み直したプロパティ数。
    ページは in-place で書き換える（has_more は False にし、読み直した印を残す）。
    """
    refetched = 0
    for value in page.get("properties", {}).values():
        if value.get("type") != "relation" or not value.get("has_more"):
            continue
        items = client.list_page_property_items(page["id"], value["id"])
        value["relation"] = [
            {"id": item["relation"]["id"]} for item in items if item.get("type") == "relation"
        ]
        value["has_more"] = False
        value["_relation_refetched"] = True
        refetched += 1
    return refetched


# ---------------------------------------------------------------------------
# 値の変換: 読み取り形式 → 書き込み形式
# ---------------------------------------------------------------------------


def _rich_text_to_write(items: Iterable[dict]) -> list[dict]:
    out = []
    for item in items or []:
        kind = item.get("type", "text")
        entry: dict[str, Any] = {"type": kind}
        if kind == "text":
            text = item.get("text") or {}
            entry["text"] = {"content": text.get("content", item.get("plain_text", ""))}
            if text.get("link"):
                entry["text"]["link"] = text["link"]
        else:  # mention / equation は読み取り形式の中身がそのまま書ける
            entry[kind] = item.get(kind)
        if item.get("annotations"):
            entry["annotations"] = item["annotations"]
        out.append(entry)
    return out


def to_write_value(value: dict) -> dict | None:
    """1 プロパティの値を pages.update に渡せる形へ変換する。書き込めない型は None。"""
    kind = value.get("type")
    if kind in READ_ONLY_TYPES:
        return None
    raw = value.get(kind)
    if kind in ("title", "rich_text"):
        return {kind: _rich_text_to_write(raw)}
    if kind == "relation":
        return {"relation": [{"id": r["id"]} for r in raw or []]}
    if kind in ("select", "status"):
        return {kind: {"name": raw["name"]} if raw else None}
    if kind == "multi_select":
        return {"multi_select": [{"name": o["name"]} for o in raw or []]}
    if kind == "date":
        if not raw:
            return {"date": None}
        return {"date": {k: raw.get(k) for k in ("start", "end", "time_zone") if raw.get(k)}}
    if kind in ("number", "checkbox", "url", "email", "phone_number"):
        return {kind: raw}
    if kind == "people":
        return {"people": [{"id": p["id"]} for p in raw or []]}
    raise UnsupportedPropertyError(f"書き戻し方が未定義の型: {kind}")


def writable_properties(page: dict) -> dict[str, dict]:
    """ページの書き込み可能なプロパティを書き込み形式で返す。"""
    out = {}
    for name, value in page.get("properties", {}).items():
        if value.get("type") == "relation" and value.get("has_more"):
            raise RuntimeError(f"relation が未取得のまま: page={page['id']} prop={name}")
        write = to_write_value(value)
        if write is not None:
            out[name] = write
    return out


def _norm_id(page_id: str) -> str:
    return str(page_id).replace("-", "").lower()


def _comparable(write_value: dict) -> Any:
    """同じ値かを比べる形。relation は順序と id の書式を無視する。"""
    if "relation" in write_value:
        return sorted({_norm_id(r["id"]) for r in write_value["relation"]})
    return write_value


def same_value(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    return json.dumps(_comparable(a), sort_keys=True, ensure_ascii=False) == json.dumps(
        _comparable(b), sort_keys=True, ensure_ascii=False
    )


def doc_id_of(page: dict) -> str:
    value = page.get("properties", {}).get(S.DISC_PROP_DOC_ID) or {}
    return "".join(i.get("plain_text", "") for i in value.get("rich_text") or []).strip()


# ---------------------------------------------------------------------------
# 計画
# ---------------------------------------------------------------------------


def value_source_page(pages: list[dict], canonical: dict) -> dict:
    """値を持ってくるコピー = last_edited_time が最新のページ。

    last_edited_time は分単位なので同着がありうる。同着なら正のページを選ぶ（更新が要らない）。
    正が同着に居なければ、PR #46 の順序キーで最も新しいページ（一番最後に作られた）を選ぶ。
    """
    latest = max(p["last_edited_time"] for p in pages)
    tied = [p for p in pages if p["last_edited_time"] == latest]
    if any(p["id"] == canonical["id"] for p in tied):
        return canonical
    return max(tied, key=upsert._page_order_key)


def _relation_union(pages_in_order: list[dict], prop: str) -> list[dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    for page in pages_in_order:
        value = page.get("properties", {}).get(prop)
        if not value or value.get("type") != "relation":
            continue
        for rel in value.get("relation") or []:
            key = _norm_id(rel["id"])
            if key not in seen:
                seen.add(key)
                merged.append({"id": rel["id"]})
    return merged


def plan_group(doc_id: str, pages: list[dict]) -> dict:
    """1 つの書類管理番号のグループの計画を作る。pages は relation 全件化済みであること。"""
    canonical = upsert.oldest_page(pages)
    if canonical is None or any(upsert._page_order_key(p) is None for p in pages):
        raise RuntimeError(f"created_time の無いページがあり正を決められない: {doc_id}")
    source = value_source_page(pages, canonical)
    # 和集合の並び: 正のページの既存リンクを先頭に、残りは古い順
    ordered = sorted(pages, key=upsert._page_order_key)

    current = writable_properties(canonical)
    desired = {k: v for k, v in writable_properties(source).items() if k not in RELATION_PROPS}
    relation_added: dict[str, list[str]] = {}
    for prop in RELATION_PROPS:
        if prop not in canonical.get("properties", {}) and not any(
            prop in p.get("properties", {}) for p in pages
        ):
            continue
        union = _relation_union(ordered, prop)
        desired[prop] = {"relation": union}
        have = set(_comparable(current.get(prop) or {"relation": []}))
        added = [r["id"] for r in union if _norm_id(r["id"]) not in have]
        if added:
            relation_added[prop] = added

    update = {k: v for k, v in desired.items() if not same_value(current.get(k), v)}
    archive = [p["id"] for p in ordered if p["id"] != canonical["id"]]
    _assert_canonical_not_archived(canonical["id"], archive)
    return {
        "doc_id": doc_id,
        "canonical_page_id": canonical["id"],
        "value_source_page_id": source["id"],
        "pages": [
            {"id": p["id"], "created_time": p["created_time"],
             "last_edited_time": p["last_edited_time"]}
            for p in ordered
        ],
        "desired_properties": desired,
        "update_properties": update,
        "relation_added": relation_added,
        "archive_page_ids": archive,
    }


def build_plan(pages: Iterable[dict], *, database_id: str, backup: dict) -> dict:
    groups_by_doc: dict[str, list[dict]] = defaultdict(list)
    rows = 0
    for page in pages:
        rows += 1
        if page.get("archived") or page.get("in_trash"):
            continue
        doc = doc_id_of(page)
        if doc:
            groups_by_doc[doc].append(page)
    groups = [
        plan_group(doc, ps) for doc, ps in sorted(groups_by_doc.items()) if len(ps) > 1
    ]
    moves: dict[str, int] = {prop: 0 for prop in RELATION_PROPS}
    for g in groups:
        for prop, ids in g["relation_added"].items():
            moves[prop] = moves.get(prop, 0) + len(ids)
    multi_master = sum(
        1 for g in groups
        if len((g["desired_properties"].get(S.PROP_MASTER_RELATION) or {}).get("relation", [])) > 1
    )
    summary = {
        "rows_in_backup": rows,
        "groups": len(groups),
        "pages_in_groups": sum(len(g["pages"]) for g in groups),
        "updates": sum(1 for g in groups if g["update_properties"]),
        "archives": sum(len(g["archive_page_ids"]) for g in groups),
        "relation_links_moved_to_canonical": moves,
        "groups_value_source_is_not_canonical": sum(
            1 for g in groups if g["value_source_page_id"] != g["canonical_page_id"]
        ),
        "groups_with_multiple_master_links_after_union": multi_master,
    }
    summary["apply_estimate"] = estimate_apply(summary)
    return {
        "version": PLAN_VERSION,
        "created_at": _iso(datetime.now(UTC)),
        "database_id": database_id,
        "backup": backup,
        "summary": summary,
        "groups": groups,
    }


def estimate_apply(summary: dict, rps: float = DEFAULT_RPS) -> dict:
    """適用のリクエスト数と所要時間の見積り（再試行・relation の追加読みは含まない）。

    グループごとに キー検索 1 + （更新があれば 更新 1 + 読み直し 1）+ アーカイブ件数。
    """
    requests_ = summary["groups"] + 2 * summary["updates"] + summary["archives"]
    return {"requests": requests_, "rps": rps, "minutes": round(requests_ / rps / 60, 1)}


# ---------------------------------------------------------------------------
# バックアップ
# ---------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_backup(pages: list[dict], backup_dir: Path, *, now: datetime, database_id: str,
                 refetched_relations: int) -> dict:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"disclosures-full-{stamp}.jsonl.gz"
    if path.exists():
        raise FileExistsError(f"同名のバックアップが既にある（上書きしない）: {path}")
    ordered = sorted(pages, key=lambda p: (p["created_time"], _norm_id(p["id"])))
    # mtime=0 で gzip ヘッダを固定し、同じ内容なら同じ sha256 になるようにする
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        for page in ordered:
            gz.write(json.dumps(page, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            gz.write(b"\n")
    info = {
        "path": str(path),
        "sha256": sha256_of(path),
        "rows": len(ordered),
        "bytes": path.stat().st_size,
        "taken_at": _iso(now),
        "database_id": database_id,
        "relations_refetched": refetched_relations,
    }
    _append_readme(backup_dir, info)
    return info


def _append_readme(backup_dir: Path, info: dict) -> None:
    readme = backup_dir / "README.md"
    header = """# Notion ④開示書類 全件バックアップ

## なぜ
④開示書類に同じ書類管理番号のページが重複している（2026-09-13 監査: 423 docID・余分 1,312 行。
95% は事前マップの窓が UTC だった不具合（stockStock PR #46 で修正済み）による実行ごとの増殖）。
ユーザの決定（2026-09-13）で、DB を作り直さず今の DB を掃除する。掃除の前に全件をここへ退避する。
掃除は stockStock の `scripts/dedupe_notion_disclosures.py` で行う。

## 中身
- `disclosures-full-<UTC時刻>.jsonl.gz`: 1 行 = ④ の 1 ページ（Notion API 2022-06-28 のページオブジェクト）。
  `id` / `created_time` / `last_edited_time` / `archived` / `properties`（生 JSON）を含む。
  relation は 25 件を超えるとページオブジェクトに載らないため、その場合はプロパティ取得 API で
  全件を読み直して置き換えてある（その値に `"_relation_refetched": true` が付く）。
- formula / rollup などの計算値も生 JSON のまま入っている（戻すときは書き込まない）。

## 戻し方
1. アーカイブしたページ: 適用はページを archive（Notion のゴミ箱へ移す）するだけで完全削除はしない。
   Notion のゴミ箱から復元する。多数なら API で `PATCH /v1/pages/<id>` に `{"archived": false}`
   （対象 id は計画ファイルの `groups[].archive_page_ids`）。
2. 正のページで上書きした値: このファイルから該当 `id` の行を取り出し、
   `scripts/dedupe_notion_disclosures.py` の `writable_properties(page)` で書き込み形式に変換して
   `pages.update` する（relation も含めて適用前の姿に戻る）。
3. ゴミ箱を空にした後は Notion からは戻せない。その場合もこのファイルから行を作り直せる
   （ただし page id と、①⑤側の逆向き relation の相手は新しいページになる）。

## 取得履歴
"""
    entry = (
        f"\n### {info['taken_at']}\n"
        f"- ファイル: `{Path(info['path']).name}`\n"
        f"- 件数: {info['rows']:,} ページ（DB {info['database_id']}）\n"
        f"- sha256: `{info['sha256']}`\n"
        f"- サイズ: {info['bytes']:,} bytes\n"
        f"- relation を全件読み直したプロパティ数: {info['relations_refetched']}\n"
        f"- 確認: `shasum -a 256 {Path(info['path']).name}`、"
        f"件数は `gzcat {Path(info['path']).name} | wc -l`\n"
    )
    text = readme.read_text(encoding="utf-8") if readme.exists() else header
    readme.write_text(text + entry, encoding="utf-8")


def read_backup(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# 適用
# ---------------------------------------------------------------------------


def _assert_canonical_not_archived(canonical_id: str, archive_ids: Iterable[str]) -> None:
    if _norm_id(canonical_id) in {_norm_id(i) for i in archive_ids}:
        raise PlanError(f"正のページがアーカイブ対象に入っている: {canonical_id}")


def validate_plan(plan: dict, *, check_backup: bool = True) -> None:
    """適用前に計画が壊れていないか確かめる。1 つでもおかしければ何も書かない。"""
    if plan.get("version") != PLAN_VERSION:
        raise PlanError(f"計画の版が違う: {plan.get('version')}")
    backup = plan.get("backup") or {}
    if check_backup:
        path = Path(backup.get("path", ""))
        if not path.is_file():
            raise PlanError(f"計画が参照するバックアップが無い（先にバックアップする）: {path}")
        if sha256_of(path) != backup.get("sha256"):
            raise PlanError(f"バックアップの sha256 が計画と一致しない: {path}")
    canonical_ids: set[str] = set()
    archive_ids: set[str] = set()
    for g in plan["groups"]:
        pages = g["pages"]
        oldest = upsert.oldest_page(pages)
        if oldest is None or _norm_id(oldest["id"]) != _norm_id(g["canonical_page_id"]):
            raise PlanError(f"正のページが PR #46 の順序キーの最古と一致しない: {g['doc_id']}")
        _assert_canonical_not_archived(g["canonical_page_id"], g["archive_page_ids"])
        members = {_norm_id(p["id"]) for p in pages}
        if not {_norm_id(i) for i in g["archive_page_ids"]} <= members:
            raise PlanError(f"グループ外のページがアーカイブ対象に入っている: {g['doc_id']}")
        canonical_ids.add(_norm_id(g["canonical_page_id"]))
        archive_ids.update(_norm_id(i) for i in g["archive_page_ids"])
    if canonical_ids & archive_ids:
        raise PlanError("別グループの正のページがアーカイブ対象に入っている")


@dataclass
class ApplyReport:
    applied: list[str] = field(default_factory=list)  # 更新とアーカイブまで終えた
    already_done: list[str] = field(default_factory=list)  # 前回の実行で収束済み
    skipped: list[dict] = field(default_factory=list)  # 計画後の変化などで飛ばした
    errors: list[dict] = field(default_factory=list)  # 書き込み・確認の失敗
    updated_pages: int = 0
    archived_pages: int = 0

    def as_dict(self) -> dict:
        return {
            "applied": self.applied, "already_done": self.already_done,
            "skipped": self.skipped, "errors": self.errors,
            "updated_pages": self.updated_pages, "archived_pages": self.archived_pages,
        }


def _archive_copy(client: NotionClient, group: dict, page_id: str) -> None:
    """アーカイブの唯一の経路。正のページ・計画に無いページは絶対に archive しない。"""
    if _norm_id(page_id) == _norm_id(group["canonical_page_id"]):
        raise PlanError(f"正のページを archive しようとした: {page_id}")
    if _norm_id(page_id) not in {_norm_id(i) for i in group["archive_page_ids"]}:
        raise PlanError(f"計画のアーカイブ対象に無いページ: {page_id}")
    client.archive_page(page_id)


def _current_matches_desired(page: dict, desired: dict) -> bool:
    current = writable_properties(page)
    return all(same_value(current.get(name), value) for name, value in desired.items())


def apply_group(client: NotionClient, db_id: str, group: dict, report: ApplyReport) -> None:
    doc_id = group["doc_id"]
    planned = {_norm_id(p["id"]): p for p in group["pages"]}
    canonical_key = _norm_id(group["canonical_page_id"])

    def skip(reason: str, **extra) -> None:
        report.skipped.append({"doc_id": doc_id, "reason": reason, **extra})

    live = client.query_database(
        db_id, filter=upsert.disclosure_filter(doc_id), sorts=upsert.KEY_QUERY_SORTS, strict=True,
    )
    live_by_id = {_norm_id(p["id"]): p for p in live}
    new_pages = [p["id"] for k, p in live_by_id.items() if k not in planned]
    if new_pages:
        return skip("計画後に同じ書類管理番号のページが増えた", pages=new_pages)
    canonical = live_by_id.get(canonical_key)
    if canonical is None:
        return skip("正のページが見つからない（archive された・書類管理番号が変わった）")
    if _norm_id(upsert.oldest_page(live)["id"]) != canonical_key:
        return skip("正のページが最古ではなくなった")
    edited = [
        p["id"] for k, p in live_by_id.items()
        if k != canonical_key and p["last_edited_time"] != planned[k]["last_edited_time"]
    ]
    if edited:
        return skip("計画後にコピーが編集された", pages=edited)

    desired = group["desired_properties"]
    # 関連付けは相手側（①銘柄マスタ・⑤原本の逆向きプロパティ）からも張れる。相手側からの変更で
    # ④ の last_edited_time が進むかは Notion の文書に無く、上の照合だけでは「計画後にコピーへ
    # 張られた原本」を見落として、そのコピーごとゴミ箱へ送りうる。そこで生きている全ページの
    # 関連付けが計画の和集合に収まっているかを確かめる。ページは上のキー検索で読んでいるので、
    # 追加のリクエストは has_more が立ったときだけ。
    # 採らなかった案: 生きている和集合で計画の値を上書きして進める → 計画・レビューの外の値を書くことになる。
    for live_page in live:
        fill_relations(client, live_page)
    for prop in RELATION_PROPS:
        planned_ids = {_norm_id(r["id"]) for r in (desired.get(prop) or {}).get("relation", [])}
        live_ids = {
            _norm_id(r["id"])
            for p in live
            for r in (p.get("properties", {}).get(prop) or {}).get("relation") or []
        }
        extra = sorted(live_ids - planned_ids)
        if extra:
            return skip("計画後に関連付けが増えた", prop=prop, relation_ids=extra)

    canonical_unchanged = (
        canonical["last_edited_time"] == planned[canonical_key]["last_edited_time"]
    )
    matches = _current_matches_desired(canonical, desired)
    if not canonical_unchanged and not matches:
        return skip("計画後に正のページが編集された", pages=[canonical["id"]])

    if not matches:
        update = {
            name: value for name, value in desired.items()
            if not same_value(writable_properties(canonical).get(name), value)
        }
        try:
            client.update_page(canonical["id"], update)
            report.updated_pages += 1
            reread = client.get_page(canonical["id"])
            fill_relations(client, reread)
        except Exception as exc:  # noqa: BLE001 - 1 グループの失敗で全体を止めない
            report.errors.append({"doc_id": doc_id, "stage": "update", "error": str(exc)})
            return
        if not _current_matches_desired(reread, desired):
            report.errors.append({
                "doc_id": doc_id, "stage": "verify",
                "error": "更新後の読み直しが計画の値と一致しない。コピーは archive しない",
            })
            return

    copies = [p["id"] for k, p in live_by_id.items() if k != canonical_key]
    if not copies and matches:
        report.already_done.append(doc_id)
        return
    for page_id in copies:
        try:
            _archive_copy(client, group, page_id)
            report.archived_pages += 1
        except PlanError:
            raise
        except Exception as exc:  # noqa: BLE001
            report.errors.append({
                "doc_id": doc_id, "stage": "archive", "page_id": page_id, "error": str(exc),
            })
            return
    report.applied.append(doc_id)


def apply_plan(client: NotionClient, plan: dict, *, check_backup: bool = True) -> ApplyReport:
    validate_plan(plan, check_backup=check_backup)
    report = ApplyReport()
    total = len(plan["groups"])
    for i, group in enumerate(plan["groups"], 1):
        apply_group(client, plan["database_id"], group, report)
        if i % 25 == 0 or i == total:
            logger.info(
                "適用 %d/%d グループ（更新 %d・archive %d・飛ばし %d・失敗 %d）",
                i, total, report.updated_pages, report.archived_pages,
                len(report.skipped), len(report.errors),
            )
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _client_and_db(args) -> tuple[NotionClient, str]:
    env = _load_env(Path(args.env_file))
    token = os.environ.get("NOTION_TOKEN") or env.get("NOTION_TOKEN")
    db_ids = json.loads(Path(args.db_ids).read_text(encoding="utf-8"))
    return NotionClient(token, rps=args.rps), db_ids["disclosures"]


def _print_summary(summary: dict) -> None:
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", default=str(root / ".env"))
    parser.add_argument("--db-ids", default=str(root / "db_ids.json"))
    parser.add_argument("--rps", type=float, default=DEFAULT_RPS)
    parser.add_argument("--backup-dir", default=str(
        Path.home() / "stockStock-backup-20260913" / "notion-disclosures"))
    parser.add_argument("--plan-out", help="計画 JSON の書き出し先")
    parser.add_argument("--from-backup", help="走査せず、既存のバックアップから計画だけ作り直す")
    parser.add_argument("--apply", action="store_true", help="計画を本番 Notion へ適用する")
    parser.add_argument("--plan", help="--apply で使う計画 JSON")
    parser.add_argument("--report-out", help="--apply の結果 JSON（既定: 計画の隣）")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.apply:
        if not args.plan:
            parser.error("--apply には --plan が必要")
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        client, db_id = _client_and_db(args)
        if _norm_id(plan["database_id"]) != _norm_id(db_id):
            raise PlanError("計画の DB と db_ids.json の ④ が一致しない")
        started = time.monotonic()
        report = apply_plan(client, plan)
        out = Path(args.report_out or f"{args.plan}.apply-"
                   f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
        result = {**report.as_dict(), "seconds": round(time.monotonic() - started)}
        out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in result.items()},
                         ensure_ascii=False))
        print(f"結果: {out}")
        return 1 if report.errors else 0

    if not args.plan_out:
        parser.error("--plan-out が必要")
    if args.from_backup:
        path = Path(args.from_backup)
        pages = read_backup(path)
        db_ids = json.loads(Path(args.db_ids).read_text(encoding="utf-8"))
        backup = {"path": str(path.resolve()), "sha256": sha256_of(path), "rows": len(pages),
                  "database_id": db_ids["disclosures"]}
    else:
        client, db_id = _client_and_db(args)
        now = datetime.now(UTC)
        meta = client.retrieve_database(db_id)
        floor = _parse_ts(meta["created_time"]) - timedelta(days=1)
        started = time.monotonic()
        scanned = scan_all_pages(client, db_id, floor=floor, ceiling=now + timedelta(minutes=5))
        pages = [copy.deepcopy(p) for p in scanned.values()]
        refetched = sum(fill_relations(client, p) for p in pages)
        logger.info("走査 %d ページ（%.0f 秒）", len(pages), time.monotonic() - started)
        backup = write_backup(pages, Path(args.backup_dir).expanduser(), now=now,
                              database_id=db_id, refetched_relations=refetched)
        print(f"バックアップ: {backup['path']} ({backup['rows']:,} 行, sha256 {backup['sha256']})")
    plan = build_plan(pages, database_id=backup["database_id"], backup=backup)
    validate_plan(plan)
    Path(args.plan_out).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    _print_summary(plan["summary"])
    print(f"計画: {args.plan_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
