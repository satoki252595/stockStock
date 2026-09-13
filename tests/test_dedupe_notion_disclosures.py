"""④開示書類の既存重複を集約するツール (scripts/dedupe_notion_disclosures.py)。

実 Notion へは接続しない。ページオブジェクトの形（読み取り形式）を持つフェイク DB で、
計画の中身と適用の順序・飛ばし・再実行を確かめる。
"""

from __future__ import annotations

import copy
import gzip
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jp_stock_pipeline.notion import schema as S
from jp_stock_pipeline.notion import upsert
from jp_stock_pipeline.notion.client import QueryTruncatedError

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "dedupe_notion_disclosures.py"
_spec = importlib.util.spec_from_file_location("dedupe_notion_disclosures", _PATH)
dd = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dd  # dataclass が自分のモジュールを引くため
_spec.loader.exec_module(dd)

DB = "db-disc"


# ---------------------------------------------------------------------------
# 読み取り形式のページを組み立てる
# ---------------------------------------------------------------------------


def _text(kind: str, value: str) -> dict:
    return {"id": kind, "type": kind, kind: [{
        "type": "text", "text": {"content": value, "link": None}, "plain_text": value,
        "href": None, "annotations": {"bold": False, "color": "default"},
    }]}


def _rel(prop_id: str, ids: list[str], has_more: bool = False) -> dict:
    return {"id": prop_id, "type": "relation", "relation": [{"id": i} for i in ids],
            "has_more": has_more}


def page(pid: str, *, created: str, edited: str, title: str = "決算短信", doc: str = "TD1",
         master: list[str] | None = None, raw: list[str] | None = None,
         factor: float | None = None, url: str | None = "https://example.invalid/a",
         code: str = "", xbrl: bool = True) -> dict:
    return {
        "object": "page", "id": pid, "created_time": created, "last_edited_time": edited,
        "archived": False,
        "properties": {
            S.DISC_PROP_TITLE: _text("title", title),
            S.DISC_PROP_DOC_ID: _text("rich_text", doc),
            S.DISC_PROP_CODE: (_text("rich_text", code) if code
                               else {"id": "k", "type": "rich_text", "rich_text": []}),
            S.DISC_PROP_SPLIT_FACTOR: {"id": "f", "type": "number", "number": factor},
            S.DISC_PROP_URL: {"id": "u", "type": "url", "url": url},
            S.DISC_PROP_DOC_TYPE: {"id": "t", "type": "select",
                                   "select": {"id": "x", "name": "決算短信", "color": "red"}},
            S.DISC_PROP_DISCLOSED_AT: {"id": "d", "type": "date", "date": {
                "start": "2026-09-13T15:00:00.000+09:00", "end": None, "time_zone": None}},
            S.DISC_PROP_HAS_XBRL: {"id": "x", "type": "checkbox", "checkbox": xbrl},
            S.PROP_MASTER_RELATION: _rel("m", master or []),
            S.PROP_RAW_RELATION: _rel("r", raw or []),
            "計算列": {"id": "c", "type": "formula", "formula": {"type": "string", "string": "z"}},
        },
    }


def title_of(p: dict) -> str:
    return p["properties"][S.DISC_PROP_TITLE]["title"][0]["text"]["content"]


def rel_ids(p: dict, prop: str) -> set[str]:
    return {r["id"] for r in p["properties"][prop]["relation"]}


class FakeNotion:
    """読み取り形式のページを持ち、pages.update の書き込み形式を読み取り形式へ戻して保存する。"""

    def __init__(self, pages: list[dict], *, full_relations: dict | None = None):
        self.pages = {p["id"]: copy.deepcopy(p) for p in pages}
        # has_more のプロパティの全件 {(page_id, prop_id): [ids]}
        self.full_relations = full_relations or {}
        self.calls: list[tuple] = []
        self.clock = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
        self.fail_archive_after: int | None = None
        self.corrupt_update = False
        self.truncate_windows_longer_than: timedelta | None = None

    def _tick(self) -> str:
        self.clock += timedelta(minutes=1)
        return self.clock.strftime("%Y-%m-%dT%H:%M:00.000Z")

    def _out(self, p: dict) -> dict:
        out = copy.deepcopy(p)
        for value in out["properties"].values():
            if value["type"] == "relation" and len(value["relation"]) > 25:
                value["relation"] = value["relation"][:25]
                value["has_more"] = True
        return out

    # -- 読み取り
    def retrieve_database(self, db_id):
        return {"created_time": "2026-06-01T00:00:00.000Z"}

    def query_database(self, db_id, *, filter=None, sorts=None, strict=False, **kw):
        self.calls.append(("query", json.dumps(filter, ensure_ascii=False)))
        rows = [p for p in self.pages.values() if not p["archived"]]
        if filter and "and" in filter:  # created_time の窓
            start = filter["and"][0]["created_time"]["on_or_after"]
            end = filter["and"][1]["created_time"]["before"]
            if self.truncate_windows_longer_than and (
                dd._parse_ts(end) - dd._parse_ts(start) > self.truncate_windows_longer_than
            ) and any(start <= p["created_time"] < end for p in rows):
                raise QueryTruncatedError("打ち切り")
            rows = [p for p in rows if start <= p["created_time"] < end]
        elif filter:  # 書類管理番号
            assert filter == upsert.disclosure_filter(filter["rich_text"]["equals"])
            rows = [p for p in rows if dd.doc_id_of(p) == filter["rich_text"]["equals"]]
        if sorts is not None:
            assert sorts == upsert.KEY_QUERY_SORTS
            rows = sorted(reversed(rows), key=lambda p: p["created_time"])
        return [self._out(p) for p in rows]

    def get_page(self, page_id):
        self.calls.append(("get", page_id))
        return self._out(self.pages[page_id])

    def list_page_property_items(self, page_id, property_id):
        self.calls.append(("prop", page_id, property_id))
        ids = self.full_relations.get((page_id, property_id))
        if ids is None:
            prop = next(v for v in self.pages[page_id]["properties"].values()
                        if v["id"] == property_id)
            ids = [r["id"] for r in prop["relation"]]
        return [{"object": "property_item", "type": "relation", "relation": {"id": i}}
                for i in ids]

    # -- 書き込み
    def update_page(self, page_id, properties):
        self.calls.append(("update", page_id))
        target = self.pages[page_id]["properties"]
        for name, write in properties.items():
            kind = next(iter(write))
            value = target[name]
            if kind in ("title", "rich_text"):
                value[kind] = [{**i, "plain_text": i["text"]["content"]} for i in write[kind]]
            elif kind == "relation":
                ids = [r["id"] for r in write["relation"]]
                if self.corrupt_update:
                    ids = ids[:1]
                value["relation"] = [{"id": i} for i in ids]
                value["has_more"] = False
            else:
                value[kind] = write[kind]
        self.pages[page_id]["last_edited_time"] = self._tick()
        return {"id": page_id}

    def archive_page(self, page_id):
        if self.fail_archive_after is not None:
            if self.fail_archive_after == 0:
                self.calls.append(("archive_failed", page_id))
                raise RuntimeError("Notion API リトライ枯渇")
            self.fail_archive_after -= 1
        self.calls.append(("archive", page_id))
        self.pages[page_id]["archived"] = True
        self.pages[page_id]["last_edited_time"] = self._tick()
        return {"id": page_id, "archived": True}

    def archived_ids(self) -> list[str]:
        return [c[1] for c in self.calls if c[0] == "archive"]


def three_copies() -> list[dict]:
    """最古 A は古い値。B が最後に編集された。C は同じ分に作られた重複。"""
    return [
        page("bbbb", created="2026-07-01T00:05:00.000Z", edited="2026-09-01T10:00:00.000Z",
             title="決算短信（訂正後）", master=["m1"], raw=["r2"], factor=3.0),
        page("aaaa", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z",
             title="決算短信", master=["m1"], raw=["r1"]),
        page("cccc", created="2026-07-01T00:05:00.000Z", edited="2026-08-01T00:00:00.000Z",
             title="決算短信（途中）", master=[], raw=["r3", "r1"]),
        page("zzzz", created="2026-07-02T00:00:00.000Z", edited="2026-07-02T00:00:00.000Z",
             doc="TD2"),  # 重複でないページは計画に入らない
    ]


def make_plan(pages: list[dict]) -> dict:
    return dd.build_plan(pages, database_id=DB, backup={"path": "x", "sha256": "y"})


# ---------------------------------------------------------------------------
# 計画
# ---------------------------------------------------------------------------


class TestCanonicalChoice:
    @pytest.mark.parametrize("reverse", [False, True])
    def test_matches_pr46_order_key_even_for_same_minute(self, reverse):
        # 同じ分に作られた 2 ページは page id の辞書順で最小が正（PR #46 と同じ規則）
        pages = [
            page("f0-2", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z"),
            page("f0-1", created="2026-07-01T00:00:00.000Z", edited="2026-09-01T00:00:00.000Z"),
            page("e9", created="2026-07-01T00:01:00.000Z", edited="2026-07-01T00:01:00.000Z"),
        ]
        if reverse:
            pages.reverse()
        plan = make_plan(pages)
        (group,) = plan["groups"]
        assert group["canonical_page_id"] == upsert.oldest_page(pages)["id"] == "f0-1"

    def test_uses_upsert_implementation_not_a_copy(self, monkeypatch):
        seen = []
        real = upsert.oldest_page

        def spy(pages):
            seen.append(len(pages))
            return real(pages)

        monkeypatch.setattr(upsert, "oldest_page", spy)
        make_plan(three_copies())
        assert seen  # 計画は upsert.oldest_page を通って正を決めている


class TestPlanValues:
    def test_last_edited_copy_values_are_written_back(self):
        plan = make_plan(three_copies())
        (group,) = plan["groups"]
        assert group["doc_id"] == "TD1"
        assert group["canonical_page_id"] == "aaaa"
        assert group["value_source_page_id"] == "bbbb"
        update = group["update_properties"]
        assert update[S.DISC_PROP_TITLE]["title"][0]["text"]["content"] == "決算短信（訂正後）"
        assert update[S.DISC_PROP_SPLIT_FACTOR] == {"number": 3.0}
        # 同じ値のプロパティ・計算値は送らない
        assert S.DISC_PROP_URL not in update
        assert S.DISC_PROP_DOC_ID not in update
        assert "計算列" not in group["desired_properties"]

    def test_relations_are_the_union_of_all_copies(self):
        plan = make_plan(three_copies())
        (group,) = plan["groups"]
        desired = group["desired_properties"]
        assert [r["id"] for r in desired[S.PROP_RAW_RELATION]["relation"]] == ["r1", "r2", "r3"]
        assert [r["id"] for r in desired[S.PROP_MASTER_RELATION]["relation"]] == ["m1"]
        assert group["relation_added"] == {S.PROP_RAW_RELATION: ["r2", "r3"]}
        assert S.PROP_MASTER_RELATION not in group["update_properties"]
        summary = plan["summary"]
        assert summary["relation_links_moved_to_canonical"][S.PROP_RAW_RELATION] == 2
        assert (summary["groups"], summary["updates"], summary["archives"]) == (1, 1, 2)

    def test_canonical_already_equal_is_not_updated(self):
        pages = [
            page("aaaa", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z",
                 master=["m1"], raw=["r1"]),
            page("bbbb", created="2026-07-01T00:01:00.000Z", edited="2026-09-01T00:00:00.000Z",
                 master=["m1"], raw=["r1"]),
        ]
        (group,) = make_plan(pages)["groups"]
        assert group["update_properties"] == {}
        assert group["archive_page_ids"] == ["bbbb"]

    def test_value_source_tie_prefers_canonical(self):
        same = "2026-09-01T00:00:00.000Z"
        pages = [
            page("aaaa", created="2026-07-01T00:00:00.000Z", edited=same, title="正"),
            page("bbbb", created="2026-07-01T00:01:00.000Z", edited=same, title="コピー"),
        ]
        (group,) = make_plan(pages)["groups"]
        assert group["value_source_page_id"] == "aaaa"
        assert group["update_properties"] == {}

    def test_empty_value_in_latest_copy_does_not_erase_existing_value(self):
        # 2026-09-13 の実例: TDnet の 2 グループで、最新のコピーだけ銘柄コードが空だった
        pages = [
            page("aaaa", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z",
                 code="1672", factor=2.0),
            page("bbbb", created="2026-07-01T00:01:00.000Z", edited="2026-09-01T00:00:00.000Z",
                 title="決算短信（訂正後）"),
        ]
        (group,) = make_plan(pages)["groups"]
        assert group["value_source_page_id"] == "bbbb"
        update = group["update_properties"]
        assert update[S.DISC_PROP_TITLE]["title"][0]["text"]["content"] == "決算短信（訂正後）"
        assert S.DISC_PROP_CODE not in update
        assert S.DISC_PROP_SPLIT_FACTOR not in update
        desired = group["desired_properties"]
        assert desired[S.DISC_PROP_CODE]["rich_text"][0]["text"]["content"] == "1672"
        assert desired[S.DISC_PROP_SPLIT_FACTOR] == {"number": 2.0}
        assert group["value_fallback"] == {
            S.DISC_PROP_CODE: "aaaa", S.DISC_PROP_SPLIT_FACTOR: "aaaa",
        }

    def test_empty_field_takes_the_newest_page_that_has_a_value(self):
        pages = [
            page("aaaa", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z",
                 code="1111"),
            page("bbbb", created="2026-07-01T00:01:00.000Z", edited="2026-08-01T00:00:00.000Z",
                 code="2222"),
            page("cccc", created="2026-07-01T00:02:00.000Z", edited="2026-09-01T00:00:00.000Z"),
        ]
        (group,) = make_plan(pages)["groups"]
        assert group["value_source_page_id"] == "cccc"
        code = group["update_properties"][S.DISC_PROP_CODE]
        assert code["rich_text"][0]["text"]["content"] == "2222"
        assert group["value_fallback"] == {S.DISC_PROP_CODE: "bbbb"}

    def test_checkbox_false_is_a_value_not_empty(self):
        pages = [
            page("aaaa", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z",
                 xbrl=True),
            page("bbbb", created="2026-07-01T00:01:00.000Z", edited="2026-09-01T00:00:00.000Z",
                 xbrl=False),
        ]
        (group,) = make_plan(pages)["groups"]
        assert group["update_properties"][S.DISC_PROP_HAS_XBRL] == {"checkbox": False}
        assert group["value_fallback"] == {}

    def test_field_empty_on_every_page_stays_empty(self):
        pages = [
            page("aaaa", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z"),
            page("bbbb", created="2026-07-01T00:01:00.000Z", edited="2026-09-01T00:00:00.000Z",
                 title="決算短信（訂正後）"),
        ]
        (group,) = make_plan(pages)["groups"]
        assert group["desired_properties"][S.DISC_PROP_CODE] == {"rich_text": []}
        assert group["desired_properties"][S.DISC_PROP_SPLIT_FACTOR] == {"number": None}
        assert group["value_fallback"] == {}

    def test_unknown_property_type_stops_instead_of_dropping_it(self):
        pages = three_copies()
        pages[0]["properties"]["謎"] = {"id": "q", "type": "files", "files": []}
        with pytest.raises(dd.UnsupportedPropertyError):
            make_plan(pages)


class TestRelationHasMore:
    def test_backup_and_plan_read_all_relation_items(self, tmp_path):
        many = [f"r{i:02d}" for i in range(30)]
        pages = [
            page("aaaa", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z",
                 raw=["r00"]),
            page("bbbb", created="2026-07-01T00:01:00.000Z", edited="2026-09-01T00:00:00.000Z",
                 raw=many),
        ]
        fake = FakeNotion(pages)
        scanned = dd.scan_all_pages(
            fake, DB, floor=datetime(2026, 6, 1, tzinfo=UTC),
            ceiling=datetime(2026, 9, 14, tzinfo=UTC),
        )
        got = {pid: copy.deepcopy(p) for pid, p in scanned.items()}
        assert got["bbbb"]["properties"][S.PROP_RAW_RELATION]["has_more"] is True
        with pytest.raises(RuntimeError, match="未取得"):
            dd.writable_properties(got["bbbb"])  # 25 件で切れたまま計画を作らない

        refetched = sum(dd.fill_relations(fake, p) for p in got.values())
        assert refetched == 1
        assert ("prop", "bbbb", "r") in fake.calls
        info = dd.write_backup(list(got.values()), tmp_path, now=fake.clock, database_id=DB,
                               refetched_relations=refetched)
        rows = dd.read_backup(Path(info["path"]))
        assert len(rows[1]["properties"][S.PROP_RAW_RELATION]["relation"]) == 30

        (group,) = make_plan(rows)["groups"]
        assert len(group["desired_properties"][S.PROP_RAW_RELATION]["relation"]) == 30

    def test_apply_verifies_relations_beyond_25(self, tmp_path):
        many = [f"r{i:02d}" for i in range(30)]
        pages = [
            page("aaaa", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z"),
            page("bbbb", created="2026-07-01T00:01:00.000Z", edited="2026-09-01T00:00:00.000Z",
                 raw=many),
        ]
        fake = FakeNotion(pages)
        report = dd.apply_plan(fake, make_plan(pages), check_backup=False)
        assert report.applied == ["TD1"] and not report.errors
        assert rel_ids(fake.pages["aaaa"], S.PROP_RAW_RELATION) == set(many)


class TestBackup:
    def test_scan_splits_truncated_windows_and_readme_records_hash(self, tmp_path):
        pages = [
            page(f"p{i}", created=f"2026-07-0{1 + i % 3}T00:0{i}:00.000Z",
                 edited="2026-07-01T00:00:00.000Z", doc=f"TD{i}")
            for i in range(6)
        ]
        fake = FakeNotion(pages)
        fake.truncate_windows_longer_than = timedelta(hours=1)
        scanned = dd.scan_all_pages(
            fake, DB, floor=datetime(2026, 6, 30, tzinfo=UTC),
            ceiling=datetime(2026, 7, 10, tzinfo=UTC),
        )
        assert set(scanned) == {p["id"] for p in pages}

        info = dd.write_backup(list(scanned.values()), tmp_path, now=fake.clock,
                               database_id=DB, refetched_relations=0)
        assert info["rows"] == 6
        with gzip.open(info["path"], "rt", encoding="utf-8") as fh:
            first = json.loads(fh.readline())
        assert {"id", "created_time", "last_edited_time", "properties"} <= set(first)
        readme = (tmp_path / "README.md").read_text(encoding="utf-8")
        assert info["sha256"] in readme and "戻し方" in readme and "6 ページ" in readme
        with pytest.raises(FileExistsError):
            dd.write_backup(list(scanned.values()), tmp_path, now=fake.clock,
                            database_id=DB, refetched_relations=0)

    def test_apply_refuses_when_backup_is_missing_or_changed(self, tmp_path):
        pages = three_copies()
        info = dd.write_backup(pages, tmp_path, now=datetime(2026, 9, 13, tzinfo=UTC),
                               database_id=DB, refetched_relations=0)
        plan = dd.build_plan(pages, database_id=DB, backup=info)
        dd.validate_plan(plan)
        Path(info["path"]).write_bytes(b"broken")
        fake = FakeNotion(pages)
        with pytest.raises(dd.PlanError, match="sha256"):
            dd.apply_plan(fake, plan)
        Path(info["path"]).unlink()
        with pytest.raises(dd.PlanError, match="バックアップが無い"):
            dd.apply_plan(fake, plan)
        assert fake.calls == []


# ---------------------------------------------------------------------------
# 適用
# ---------------------------------------------------------------------------


class TestApply:
    def test_updates_canonical_then_archives_only_copies(self):
        pages = three_copies()
        fake = FakeNotion(pages)
        report = dd.apply_plan(fake, make_plan(pages), check_backup=False)
        assert report.applied == ["TD1"] and not report.skipped and not report.errors
        ops = [c[0] for c in fake.calls if c[0] in ("update", "get", "archive")]
        assert ops[:2] == ["update", "get"]  # 更新 → 読み直し → archive の順
        assert sorted(fake.archived_ids()) == ["bbbb", "cccc"]
        canonical = fake.pages["aaaa"]
        assert not canonical["archived"]
        assert title_of(canonical) == "決算短信（訂正後）"
        assert rel_ids(canonical, S.PROP_RAW_RELATION) == {"r1", "r2", "r3"}
        assert canonical["properties"][S.DISC_PROP_SPLIT_FACTOR]["number"] == 3.0

    def test_apply_keeps_existing_code_when_latest_copy_is_empty(self):
        pages = [
            page("aaaa", created="2026-07-01T00:00:00.000Z", edited="2026-07-01T00:00:00.000Z",
                 code="1672"),
            page("bbbb", created="2026-07-01T00:01:00.000Z", edited="2026-09-01T00:00:00.000Z",
                 title="決算短信（訂正後）"),
        ]
        fake = FakeNotion(pages)
        report = dd.apply_plan(fake, make_plan(pages), check_backup=False)
        assert (report.errors, report.skipped, report.applied) == ([], [], ["TD1"])
        canonical = fake.pages["aaaa"]
        assert canonical["properties"][S.DISC_PROP_CODE]["rich_text"][0]["plain_text"] == "1672"
        assert title_of(canonical) == "決算短信（訂正後）"
        assert fake.archived_ids() == ["bbbb"]

    def test_group_edited_after_plan_is_skipped(self):
        pages = three_copies()
        plan = make_plan(pages)
        fake = FakeNotion(pages)
        fake.pages["cccc"]["last_edited_time"] = "2026-09-13T23:00:00.000Z"
        report = dd.apply_plan(fake, plan, check_backup=False)
        assert report.skipped[0]["reason"] == "計画後にコピーが編集された"
        assert [c for c in fake.calls if c[0] in ("update", "archive")] == []

    def test_canonical_edited_after_plan_is_skipped(self):
        pages = three_copies()
        plan = make_plan(pages)
        fake = FakeNotion(pages)
        fake.update_page("aaaa", {S.DISC_PROP_TITLE: {"title": [
            {"type": "text", "text": {"content": "書き手が更新"}}]}})
        fake.calls.clear()
        report = dd.apply_plan(fake, plan, check_backup=False)
        assert report.skipped[0]["reason"] == "計画後に正のページが編集された"
        assert [c for c in fake.calls if c[0] in ("update", "archive")] == []
        assert title_of(fake.pages["aaaa"]) == "書き手が更新"

    def test_new_copy_after_plan_is_skipped(self):
        pages = three_copies()
        plan = make_plan(pages)
        fake = FakeNotion(pages + [page("dddd", created="2026-09-13T00:00:00.000Z",
                                         edited="2026-09-13T00:00:00.000Z")])
        report = dd.apply_plan(fake, plan, check_backup=False)
        assert report.skipped[0]["reason"] == "計画後に同じ書類管理番号のページが増えた"
        assert fake.archived_ids() == []

    def test_relation_added_from_other_side_after_plan_is_skipped(self):
        # ⑤ 側から計画後にコピーへ原本が張られ、コピーの last_edited_time は進まなかった場合。
        # そのままだとコピーごとゴミ箱へ送り、正のページからその原本が辿れなくなる。
        pages = three_copies()
        plan = make_plan(pages)
        fake = FakeNotion(pages)
        fake.pages["cccc"]["properties"][S.PROP_RAW_RELATION]["relation"].append({"id": "r9"})
        report = dd.apply_plan(fake, plan, check_backup=False)
        assert report.skipped[0]["reason"] == "計画後に関連付けが増えた"
        assert report.skipped[0]["relation_ids"] == ["r9"]
        assert [c for c in fake.calls if c[0] in ("update", "archive")] == []

    def test_verify_mismatch_does_not_archive(self):
        pages = three_copies()
        fake = FakeNotion(pages)
        fake.corrupt_update = True
        report = dd.apply_plan(fake, make_plan(pages), check_backup=False)
        assert report.errors[0]["stage"] == "verify"
        assert fake.archived_ids() == []

    def test_interrupted_run_converges_on_rerun(self):
        pages = three_copies()
        plan = make_plan(pages)
        fake = FakeNotion(pages)
        fake.fail_archive_after = 1  # 1 件目の archive の後で止まる
        first = dd.apply_plan(fake, plan, check_backup=False)
        assert first.errors and first.errors[0]["stage"] == "archive"
        assert len(fake.archived_ids()) == 1

        fake.fail_archive_after = None
        second = dd.apply_plan(fake, plan, check_backup=False)
        assert second.applied == ["TD1"] and not second.skipped and not second.errors
        assert second.updated_pages == 0  # 正は前回で更新済み。書き直さない
        assert sorted(fake.archived_ids()) == ["bbbb", "cccc"]

        before = len(fake.calls)
        third = dd.apply_plan(fake, plan, check_backup=False)
        assert third.already_done == ["TD1"]
        assert [c for c in fake.calls[before:] if c[0] in ("update", "archive")] == []


class TestCanonicalIsNeverArchived:
    def test_tampered_plan_with_canonical_in_archive_list_is_refused(self):
        pages = three_copies()
        plan = make_plan(pages)
        plan["groups"][0]["archive_page_ids"].append("aaaa")
        fake = FakeNotion(pages)
        with pytest.raises(dd.PlanError, match="正のページ"):
            dd.apply_plan(fake, plan, check_backup=False)
        assert fake.calls == []

    def test_tampered_canonical_that_is_not_oldest_is_refused(self):
        pages = three_copies()
        plan = make_plan(pages)
        group = plan["groups"][0]
        group["canonical_page_id"] = "bbbb"
        group["archive_page_ids"] = ["aaaa", "cccc"]
        with pytest.raises(dd.PlanError, match="順序キー"):
            dd.apply_plan(FakeNotion(pages), plan, check_backup=False)

    def test_archive_helper_rejects_canonical_and_pages_outside_plan(self):
        (group,) = make_plan(three_copies())["groups"]
        fake = FakeNotion(three_copies())
        with pytest.raises(dd.PlanError):
            dd._archive_copy(fake, group, "aaaa")
        with pytest.raises(dd.PlanError):
            dd._archive_copy(fake, group, "zzzz")
        assert fake.archived_ids() == []

    def test_archive_helper_rejects_canonical_even_if_listed_in_plan(self):
        # validate_plan を通らない経路（計画を読み込んだ後に書き換わった等）でも、
        # _archive_copy 自身が正のページを拒否する。上のテストは「計画外」の検査でも落ちるので、
        # 正のページの検査だけを外したときに落ちるよう、正をアーカイブ対象に入れた計画で確かめる。
        (group,) = make_plan(three_copies())["groups"]
        group["archive_page_ids"].append("aaaa")
        fake = FakeNotion(three_copies())
        with pytest.raises(dd.PlanError, match="正のページを archive"):
            dd._archive_copy(fake, group, "aaaa")
        assert fake.archived_ids() == []

    def test_validate_plan_rejects_canonical_listed_in_its_own_group(self):
        # 別グループとの突き合わせではなく、同じグループ内の検査で止まることを確かめる
        plan = make_plan(three_copies())
        plan["groups"][0]["archive_page_ids"].append("aaaa")
        with pytest.raises(dd.PlanError, match="正のページがアーカイブ対象に入っている: aaaa"):
            dd.validate_plan(plan, check_backup=False)

    def test_validate_plan_rejects_canonical_of_another_group(self):
        pages = three_copies() + [
            page("yyyy", created="2026-07-03T00:00:00.000Z", edited="2026-07-03T00:00:00.000Z",
                 doc="TD2"),
        ]
        plan = make_plan(pages)
        td1, td2 = plan["groups"]
        td1["archive_page_ids"].append(td2["canonical_page_id"])
        td1["pages"].append(next(p for p in td2["pages"] if p["id"] == td2["canonical_page_id"]))
        with pytest.raises(dd.PlanError, match="別グループの正のページ"):
            dd.validate_plan(plan, check_backup=False)

    def test_canonical_is_not_archived_across_many_runs(self):
        pages = three_copies()
        plan = make_plan(pages)
        fake = FakeNotion(pages)
        for _ in range(3):
            dd.apply_plan(fake, plan, check_backup=False)
        assert "aaaa" not in fake.archived_ids()
        assert not fake.pages["aaaa"]["archived"]


def test_estimate_counts_requests_at_2_5_rps():
    summary = {"groups": 423, "updates": 400, "archives": 1312}
    est = dd.estimate_apply(summary)
    assert est["requests"] == 423 + 800 + 1312
    assert est["minutes"] == round(est["requests"] / 2.5 / 60, 1)
