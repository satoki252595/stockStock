"""Notion の検索→作成の競合で重複ページができても 1 つに収束させる (#13)。

実 Notion へは接続しない。キー検索・作成・更新・archive を持つ小さなフェイク DB で、
書き手の実行順（交互・同時・応答喪失・再起動）を明示的に組み立てて検証する。
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta

import pytest

from conftest import notion_env

from jp_stock_pipeline.config import load_settings
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import (
    JST,
    DataQuality,
    DisclosureRecord,
    FinancialSummaryRecord,
    Provenance,
    Source,
)
from jp_stock_pipeline.notion import file_upload
from jp_stock_pipeline.notion import schema as S
from jp_stock_pipeline.notion import upsert
from jp_stock_pipeline.notion.client import NotionRequestError

ENV = notion_env()
SAME_MINUTE = "2026-09-13T00:00:00.000Z"


def settings():
    return load_settings(dry_run=True, env=ENV)


def prov() -> Provenance:
    return Provenance(
        source=Source.TDNET,
        license_tag=LicenseTag.FACTUAL_CITE,
        data_date=date(2026, 9, 13),
        fetched_at=datetime(2026, 9, 13, 9, 0, tzinfo=JST),
        raw_page_id="raw-1",
        quality=DataQuality.OK,
    )


def disclosure(title: str, doc_id: str = "TD0001") -> DisclosureRecord:
    return DisclosureRecord(
        doc_id=doc_id, title=title,
        disclosed_at=datetime(2026, 9, 13, 15, 0, tzinfo=JST), provenance=prov(),
    )


def financial(net_sales: float, disclosed_on: date | None) -> FinancialSummaryRecord:
    return FinancialSummaryRecord(
        code="7203", fiscal_period_end=date(2026, 6, 30), disclosure_type="1Q",
        net_sales=net_sales,
        disclosed_at=(
            datetime(disclosed_on.year, disclosed_on.month, disclosed_on.day, 15, tzinfo=JST)
            if disclosed_on else None
        ),
        provenance=prov(),
    )


def _matches(flt: dict | None, props: dict) -> bool:
    """フェイク用の最小フィルタ評価（キー検索で使う equals / is_empty と and / or のみ）。"""
    if flt is None:
        return True
    if "and" in flt:
        return all(_matches(sub, props) for sub in flt["and"])
    if "or" in flt:
        return any(_matches(sub, props) for sub in flt["or"])
    prop = props.get(flt["property"]) or {}
    for kind in ("rich_text", "title"):
        if kind in flt:
            items = prop.get(kind) or []
            return (items[0]["text"]["content"] if items else "") == flt[kind]["equals"]
    if "date" in flt:
        return (prop.get("date") or {}).get("start") == flt["date"]["equals"]
    if "select" in flt:
        name = (prop.get("select") or {}).get("name")
        if flt["select"].get("is_empty"):
            return name is None
        return name == flt["select"]["equals"]
    raise AssertionError(f"未対応のフィルタ: {flt}")


class FakeNotion:
    """キー検索・作成・更新・archive を持つフェイク DB。呼び出しを calls に記録する。

    created_time は既定で 1 分ずつ進む（Notion と同じく分単位の文字列）。
    - miss_queries: 次の N 回の検索は空を返す（読んだ時点では未存在だった、の再現）
    - before_insert / after_insert: create の挿入前後に 1 回だけ割り込む書き手
    """

    def __init__(self, *, ids=None, created_times=None):
        self.pages: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self._ids = iter(ids) if ids else None
        self._times = iter(created_times) if created_times else None
        self._n = 0
        self.miss_queries = 0
        self.fail_next_query = False
        self.before_insert = None
        self.after_insert = None
        self.ambiguous_create = False
        self.fail_archive = False
        self.fail_update = False

    # -- 読み取り ---------------------------------------------------------
    def query_database(self, db_id, *, filter=None, sorts=None, page_size=100,
                       max_pages=None, **kw):
        self.calls.append(("query", db_id))
        if self.fail_next_query:
            self.fail_next_query = False
            raise NotionRequestError("Notion API リトライ枯渇")
        if self.miss_queries:
            self.miss_queries -= 1
            return []
        rows = [
            p for p in self.pages
            if p["db"] == db_id and not p["archived"] and _matches(filter, p["properties"])
        ]
        if sorts is not None:
            assert sorts == upsert.KEY_QUERY_SORTS
            # 同じ時刻の並びは Notion では決まらない。挿入の逆順にして id 順に頼らせない
            rows = sorted(reversed(rows), key=lambda p: p["created_time"])
        if max_pages:
            rows = rows[: page_size * max_pages]
        return [
            {"id": p["id"], "created_time": p["created_time"],
             "properties": copy.deepcopy(p["properties"])}
            for p in rows
        ]

    # -- 書き込み ---------------------------------------------------------
    def create_page(self, *, parent, properties):
        db_id = parent["database_id"]
        self.calls.append(("create", db_id))
        hook, self.before_insert = self.before_insert, None
        if hook:
            hook()
        self._n += 1
        page_id = next(self._ids) if self._ids else f"page-{self._n:04d}"
        created_time = (
            next(self._times) if self._times else f"2026-09-13T00:{self._n:02d}:00.000Z"
        )
        self.pages.append({
            "id": page_id, "db": db_id, "created_time": created_time,
            "archived": False, "properties": copy.deepcopy(properties),
        })
        if self.ambiguous_create:
            self.ambiguous_create = False
            raise NotionRequestError("Notion 書き込みの結果不明。重複防止のため自動再送しません。")
        hook, self.after_insert = self.after_insert, None
        if hook:
            hook()
        return {"object": "page", "id": page_id, "created_time": created_time}

    def update_page(self, page_id, properties):
        self.calls.append(("update", page_id))
        if self.fail_update:
            raise NotionRequestError("Notion API リトライ枯渇")
        self._page(page_id)["properties"].update(copy.deepcopy(properties))
        return {"id": page_id}

    def archive_page(self, page_id):
        self.calls.append(("archive", page_id))
        if self.fail_archive:
            raise NotionRequestError("Notion API リトライ枯渇")
        self._page(page_id)["archived"] = True
        return {"id": page_id, "archived": True}

    # -- 検証用 -----------------------------------------------------------
    def _page(self, page_id):
        return next(p for p in self.pages if p["id"] == page_id)

    def active(self, db_id):
        return [p for p in self.pages if p["db"] == db_id and not p["archived"]]

    def archived(self, db_id):
        return [p["id"] for p in self.pages if p["db"] == db_id and p["archived"]]

    def count(self, op):
        return sum(1 for call in self.calls if call[0] == op)


def title_of(page: dict) -> str:
    return page["properties"][S.DISC_PROP_TITLE]["title"][0]["text"]["content"]


def net_sales_of(page: dict) -> float:
    return page["properties"][S.FIN_PROP_NET_SALES]["number"]


class TestConcurrentCreate:
    def test_two_writers_both_read_missing_converge_to_the_older_page(self):
        """A と B が両方「未存在」を読んで create。後から作られた A だけが archive される。"""
        fake = FakeNotion()
        results = {}
        # A の create が届く直前に B が検索→作成→再確認まで済ませる（B の検索時点で A は無い）
        fake.before_insert = lambda: results.setdefault(
            "B", upsert.upsert_disclosure(fake, settings(), disclosure("B の値"))
        )
        results["A"] = upsert.upsert_disclosure(fake, settings(), disclosure("A の値"))

        assert fake.count("create") == 2  # 両方が未存在を読んで作った
        (active,) = fake.active("db-disc")
        assert active["id"] == "page-0001"  # 先に作られた B のページが正
        assert fake.archived("db-disc") == ["page-0002"]  # archive は後から作られた A だけ
        assert title_of(active) == "A の値"  # 後から来た A の内容は正のページへ書き直された
        assert results == {"A": "page-0001", "B": "page-0001"}

    @pytest.mark.parametrize("id_a,id_b", [("bbbb", "aaaa"), ("aaaa", "bbbb")])
    def test_same_created_time_is_decided_by_page_id(self, id_a, id_b):
        """同じ分に作られ互いが見える場合、両者とも page id が小さい方を正と判断する。"""
        fake = FakeNotion(ids=[id_a, id_b], created_times=[SAME_MINUTE, SAME_MINUTE])
        outcomes = {}

        def writer_b():
            fake.miss_queries = 1  # B の検索時点では A がまだ見えなかった
            outcomes["B"] = upsert._upsert_outcome(
                fake, "db-disc", upsert.disclosure_filter("TD0001"),
                upsert.disclosure_properties(disclosure("B の値")),
            )

        fake.after_insert = writer_b  # A の作成後・A の再確認前に B が作って再確認する
        outcomes["A"] = upsert._upsert_outcome(
            fake, "db-disc", upsert.disclosure_filter("TD0001"),
            upsert.disclosure_properties(disclosure("A の値")),
        )

        winner, loser = sorted([id_a, id_b])
        (active,) = fake.active("db-disc")
        assert active["id"] == winner
        assert fake.archived("db-disc") == [loser]
        loser_name = "A" if loser == id_a else "B"
        assert title_of(active) == f"{loser_name} の値"
        assert outcomes[loser_name].archived and outcomes[loser_name].rewritten
        assert outcomes["A"].page_id == outcomes["B"].page_id == winner

    def test_writer_that_is_oldest_archives_nothing(self):
        fake = FakeNotion()
        page_id = upsert.upsert_disclosure(fake, settings(), disclosure("単独"))
        assert page_id == "page-0001"
        assert fake.calls == [("query", "db-disc"), ("create", "db-disc"), ("query", "db-disc")]
        assert fake.archived("db-disc") == []

    def test_oldest_writer_leaves_newer_duplicate_to_its_creator(self):
        """自分が正なら、後から作られた他人のページを archive しない。"""
        fake = FakeNotion()
        # A の作成後・再確認前に B が「未存在」を読んで作る（B の再確認は A の作成より後）
        fake.after_insert = lambda: (
            setattr(fake, "miss_queries", 1),
            upsert.upsert_disclosure(fake, settings(), disclosure("B の値")),
        )
        outcome = upsert._upsert_outcome(
            fake, "db-disc", upsert.disclosure_filter("TD0001"),
            upsert.disclosure_properties(disclosure("A の値")),
        )
        assert outcome.page_id == "page-0001"
        assert not outcome.archived
        # archive したのは B が自分で作ったページだけ
        assert [c for c in fake.calls if c[0] == "archive"] == [("archive", "page-0002")]

    def test_prefetched_map_create_path_is_also_checked(self):
        """事前マップで「未収録」と判断して作ったときも同じ再確認を通す。"""
        fake = FakeNotion()
        fake.before_insert = lambda: upsert.upsert_disclosure(
            fake, settings(), disclosure("B の値")
        )
        page_id = upsert.upsert_disclosure(
            fake, settings(), disclosure("A の値"), existing_page_id=None, page_resolved=True
        )
        assert page_id == "page-0001"
        assert fake.archived("db-disc") == ["page-0002"]
        assert len(fake.active("db-disc")) == 1


class TestExistingDuplicates:
    @pytest.mark.parametrize("created_times,ids,expected", [
        (["2026-09-13T00:05:00.000Z", "2026-09-13T00:00:00.000Z"], ["new", "old"], "old"),
        ([SAME_MINUTE, SAME_MINUTE], ["zzzz", "aaaa"], "aaaa"),
    ], ids=["older-created-time", "same-minute-smaller-id"])
    def test_updates_go_to_the_canonical_page_and_nothing_is_archived(
        self, created_times, ids, expected
    ):
        fake = FakeNotion(ids=ids, created_times=created_times)
        props = upsert.disclosure_properties(disclosure("既存"))
        for _ in ids:  # 呼び出し前から重複している
            fake.create_page(parent={"database_id": "db-disc"}, properties=props)
        fake.calls.clear()

        page_id = upsert.upsert_disclosure(fake, settings(), disclosure("更新"))

        assert page_id == expected
        assert fake.calls == [("query", "db-disc"), ("update", expected)]
        assert fake.archived("db-disc") == []  # もともとあったページは archive しない
        assert len(fake.active("db-disc")) == 2

    def test_key_search_asks_notion_for_oldest_first_beyond_page_size(self):
        """重複が page_size を超えても最古を取れるのは sorts のおかげ（手元の min だけでは足りない）。"""
        n = upsert.KEY_QUERY_PAGE_SIZE + 1
        # 挿入順は新しい順の逆: 最後に入れたページが最古。sorts 無しなら先頭 page_size 件に入らない
        created_times = [f"2026-09-13T00:{i + 1:02d}:00.000Z" for i in range(n - 1)]
        created_times.append("2026-09-13T00:00:00.000Z")
        ids = [f"dup-{i:02d}" for i in range(n)]
        fake = FakeNotion(ids=ids, created_times=created_times)
        props = upsert.disclosure_properties(disclosure("既存"))
        for _ in ids:
            fake.create_page(parent={"database_id": "db-disc"}, properties=props)
        fake.calls.clear()

        page_id = upsert.upsert_disclosure(fake, settings(), disclosure("更新"))

        assert page_id == ids[-1]
        assert fake.calls == [("query", "db-disc"), ("update", ids[-1])]

    @pytest.mark.parametrize("reverse", [False, True], ids=["old-last", "old-first"])
    def test_prefetched_maps_pick_the_same_canonical_page(self, reverse):
        # DB ごとにキー列の型が違うので DB ごとに作る
        key_prop = {
            "db-master": (S.MASTER_PROP_CODE, "rich_text"),
            "db-disc": (S.DISC_PROP_DOC_ID, "rich_text"),
        }
        rows = [
            ("p-new", "2026-09-13T00:05:00.000Z", "7203"),
            ("p-old", "2026-09-13T00:00:00.000Z", "7203"),
            ("p-zz", SAME_MINUTE, "6758"),
            ("p-aa", SAME_MINUTE, "6758"),
        ]
        if reverse:
            # 返る順序に依らず最古を選ぶ（「後から来た行を採る」実装では片方の順序でしか通らない）
            rows.reverse()

        class _C:
            def query_database(self, db_id, **kw):
                name, kind = key_prop[db_id]
                return [
                    {"id": page_id, "created_time": created_time,
                     "properties": {name: {kind: [{"plain_text": code}]}}}
                    for page_id, created_time, code in rows
                ]

        expected = {"7203": "p-old", "6758": "p-aa"}
        assert upsert.load_stock_master_map(_C(), settings()) == expected
        assert upsert.load_disclosure_page_map(_C(), settings()) == expected

    def test_raw_file_lookup_returns_the_oldest_and_archives_nothing(self):
        fake = FakeNotion(ids=["raw-new", "raw-old"],
                          created_times=["2026-09-13T00:05:00.000Z", "2026-09-13T00:00:00.000Z"])
        props = {S.RAW_PROP_SHA256: upsert.text_prop("a" * 64)}
        for _ in range(2):
            fake.create_page(parent={"database_id": "db-raw"}, properties=props)
        assert file_upload.find_raw_page_by_sha256(fake, settings(), "a" * 64) == "raw-old"
        assert fake.archived("db-raw") == []


class TestLostResponseAndRestart:
    def test_ambiguous_create_is_not_resent_and_next_run_updates(self):
        fake = FakeNotion()
        fake.ambiguous_create = True  # サーバは作成したが応答が失われた
        with pytest.raises(NotionRequestError, match="結果不明"):
            upsert.upsert_disclosure(fake, settings(), disclosure("1 回目"))
        assert fake.count("create") == 1  # 再送しない (#12)
        assert fake.count("archive") == 0

        fake.calls.clear()  # 再起動後の次回実行
        page_id = upsert.upsert_disclosure(fake, settings(), disclosure("2 回目"))
        assert page_id == "page-0001"
        assert fake.calls == [("query", "db-disc"), ("update", "page-0001")]
        (active,) = fake.active("db-disc")
        assert title_of(active) == "2 回目"


class TestFinancialGuardOnRewrite:
    def _race(self, a: FinancialSummaryRecord, b: FinancialSummaryRecord) -> tuple[FakeNotion, str]:
        fake = FakeNotion()
        fake.before_insert = lambda: upsert.upsert_financial_summary(fake, settings(), b)
        return fake, upsert.upsert_financial_summary(fake, settings(), a)

    def test_rewrite_does_not_roll_back_a_newer_disclosure(self):
        """後から作られた A の方が古い開示なら、正のページ (B) を書き換えない。"""
        fake, page_id = self._race(
            a=financial(999.0, date(2026, 8, 1)), b=financial(1000.0, date(2026, 9, 1))
        )
        (active,) = fake.active("db-fin")
        assert page_id == active["id"] == "page-0001"
        assert net_sales_of(active) == 1000.0  # 新しい開示の値が残る
        assert fake.archived("db-fin") == ["page-0002"]  # A のページは archive（復元可能）
        assert ("update", "page-0001") not in fake.calls

    def test_rewrite_applies_a_newer_disclosure(self):
        fake, page_id = self._race(
            a=financial(1000.0, date(2026, 9, 1)), b=financial(999.0, date(2026, 8, 1))
        )
        (active,) = fake.active("db-fin")
        assert page_id == active["id"] == "page-0001"
        assert net_sales_of(active) == 1000.0
        assert fake.archived("db-fin") == ["page-0002"]


class TestFailuresDoNotStopTheJob:
    def _race_outcome(self, fake: FakeNotion) -> upsert.UpsertOutcome:
        fake.before_insert = lambda: upsert.upsert_disclosure(
            fake, settings(), disclosure("B の値")
        )
        return upsert._upsert_outcome(
            fake, "db-disc", upsert.disclosure_filter("TD0001"),
            upsert.disclosure_properties(disclosure("A の値")),
        )

    def test_archive_failure_warns_and_keeps_the_value(self, caplog):
        fake = FakeNotion()
        fake.fail_archive = True
        with caplog.at_level("WARNING"):
            outcome = self._race_outcome(fake)
        assert outcome.page_id == "page-0001"
        assert outcome.rewritten and not outcome.archived
        assert "archive に失敗" in (outcome.warning or "")
        assert any("archive に失敗" in r.message for r in caplog.records)
        assert len(fake.active("db-disc")) == 2  # 重複は残るが値は正のページにある
        assert title_of(fake._page("page-0001")) == "A の値"

    def test_rewrite_failure_keeps_own_page_and_does_not_archive(self, caplog):
        fake = FakeNotion()
        fake.fail_update = True
        with caplog.at_level("WARNING"):
            outcome = self._race_outcome(fake)
        assert outcome.page_id == "page-0002"  # 値が入っている自分のページを返す
        assert not outcome.archived
        assert fake.count("archive") == 0
        assert "書き直しに失敗" in (outcome.warning or "")

    def test_recheck_query_failure_warns_and_returns_created_page(self, caplog):
        fake = FakeNotion()

        def fail_recheck():
            fake.fail_next_query = True

        fake.after_insert = fail_recheck
        with caplog.at_level("WARNING"):
            page_id = upsert.upsert_disclosure(fake, settings(), disclosure("A"))
        assert page_id == "page-0001"
        assert fake.count("archive") == 0
        assert any("再確認に失敗" in r.message for r in caplog.records)


class TestRequestCount:
    """ランニングコスト: 追加の問い合わせは create 1 回につき 1 回、update では 0 回。"""

    def test_update_via_search_adds_no_query(self):
        fake = FakeNotion()
        upsert.upsert_disclosure(fake, settings(), disclosure("初回"))
        fake.calls.clear()
        upsert.upsert_disclosure(fake, settings(), disclosure("更新"))
        assert fake.calls == [("query", "db-disc"), ("update", "page-0001")]

    def test_update_via_prefetched_map_adds_no_query(self):
        fake = FakeNotion()
        upsert.upsert_disclosure(fake, settings(), disclosure("初回"))
        fake.calls.clear()
        upsert.upsert_disclosure(
            fake, settings(), disclosure("更新"),
            existing_page_id="page-0001", page_resolved=True,
        )
        assert fake.calls == [("update", "page-0001")]

    def test_financial_update_adds_no_query(self):
        fake = FakeNotion()
        upsert.upsert_financial_summary(fake, settings(), financial(1.0, date(2026, 8, 1)))
        fake.calls.clear()
        upsert.upsert_financial_summary(fake, settings(), financial(2.0, date(2026, 9, 1)))
        assert fake.calls == [("query", "db-fin"), ("update", "page-0001")]

    def test_create_adds_exactly_one_query(self):
        fake = FakeNotion()
        upsert.upsert_financial_summary(fake, settings(), financial(1.0, date(2026, 8, 1)))
        assert fake.calls == [("query", "db-fin"), ("create", "db-fin"), ("query", "db-fin")]

    def test_dry_run_create_does_not_recheck_or_write(self, dry_client, monkeypatch):
        queries = []
        monkeypatch.setattr(
            dry_client, "query_database", lambda *a, **k: queries.append(1) or []
        )
        page_id = upsert.upsert_disclosure(dry_client, settings(), disclosure("dry"))
        assert page_id.startswith("dry-run-")
        assert len(queries) == 1  # 作成前の検索だけ
        assert [op.op for op in dry_client.ops] == ["create_page"]


class TestDisclosureMapWindowIsJstDay:
    """④ の事前マップの窓が JST の 1 日であること（#13 の重複の 95% の原因）。"""

    def _window(self, day):
        captured = {}

        class _Cap:
            def query_database(self, _db_id, filter=None, **_kw):
                captured["filter"] = filter
                return []

        upsert.load_disclosure_page_map(_Cap(), settings(), disclosed_date=day)
        conds = captured["filter"]["and"]
        return (
            datetime.fromisoformat(conds[0]["date"]["on_or_after"]),
            datetime.fromisoformat(conds[1]["date"]["before"]),
        )

    def test_9時前の開示が窓に入る(self) -> None:
        """JST 08:00 の開示は UTC では前日 23:00。日付だけの境界だと漏れていた。"""
        start, end = self._window(date(2026, 9, 11))
        early = datetime(2026, 9, 11, 8, 0, tzinfo=JST)
        assert start <= early < end

    def test_境界にオフセットが付いている(self) -> None:
        """日付だけの文字列は Notion が UTC の 0 時として比べるので渡さない。"""
        start, end = self._window(date(2026, 9, 11))
        assert start.utcoffset() == timedelta(hours=9)
        assert end.utcoffset() == timedelta(hours=9)
        assert end - start == timedelta(days=1)

    def test_前日の深夜と翌日の0時は窓の外(self) -> None:
        start, end = self._window(date(2026, 9, 11))
        assert not (start <= datetime(2026, 9, 10, 23, 59, tzinfo=JST) < end)
        assert not (start <= datetime(2026, 9, 12, 0, 0, tzinfo=JST) < end)


# --- ③ のキーに連結単体を足す / 「2Q」→「中間」の旧行を採用する ---------------


def fin(
    *, dtype: str = "1Q", consolidated: str | None = "連結", net_sales: float = 1.0,
    period_end: date = date(2026, 6, 30), disclosed_on: date | None = date(2026, 8, 1),
) -> FinancialSummaryRecord:
    return FinancialSummaryRecord(
        code="7203", fiscal_period_end=period_end, disclosure_type=dtype,
        consolidated=consolidated, net_sales=net_sales,
        disclosed_at=(
            datetime(disclosed_on.year, disclosed_on.month, disclosed_on.day, 15, tzinfo=JST)
            if disclosed_on else None
        ),
        provenance=prov(),
    )


def seed(fake: FakeNotion, record: FinancialSummaryRecord) -> None:
    """変更前の書き手が作った行を置く（キー検索を通さず作る）。"""
    fake.create_page(
        parent={"database_id": "db-fin"},
        properties=upsert.financial_summary_properties(record),
    )
    fake.calls.clear()


def select_of(page: dict, prop: str) -> str | None:
    return (page["properties"][prop].get("select") or {}).get("name")


class TestFinancialKeyIncludesConsolidation:
    def test_consolidated_and_standalone_coexist(self):
        fake = FakeNotion()
        upsert.upsert_financial_summary(fake, settings(), fin(consolidated="連結", net_sales=1000.0))
        upsert.upsert_financial_summary(fake, settings(), fin(consolidated="単体", net_sales=400.0))
        upsert.upsert_financial_summary(
            fake, settings(),
            fin(consolidated="連結", net_sales=1100.0, disclosed_on=date(2026, 9, 1)),
        )
        by_scope = {
            select_of(p, S.FIN_PROP_CONSOLIDATED): net_sales_of(p) for p in fake.active("db-fin")
        }
        assert by_scope == {"連結": 1100.0, "単体": 400.0}  # 後勝ちで潰れない
        assert fake.archived("db-fin") == []

    def test_concurrent_creates_of_both_scopes_do_not_archive_each_other(self):
        """作成直後の再確認 (#13) は正確なキーで行う。別の連結区分は重複ではない。"""
        fake = FakeNotion()
        fake.before_insert = lambda: upsert.upsert_financial_summary(
            fake, settings(), fin(consolidated="単体", net_sales=400.0)
        )
        upsert.upsert_financial_summary(fake, settings(), fin(consolidated="連結", net_sales=1000.0))
        assert len(fake.active("db-fin")) == 2
        assert fake.archived("db-fin") == []

    def test_a_legacy_row_without_consolidation_is_adopted(self):
        fake = FakeNotion()
        seed(fake, fin(consolidated=None, net_sales=900.0))
        page_id = upsert.upsert_financial_summary(
            fake, settings(), fin(consolidated="連結", net_sales=1000.0)
        )
        (active,) = fake.active("db-fin")  # 重複を作らない
        assert page_id == active["id"] == "page-0001"
        assert select_of(active, S.FIN_PROP_CONSOLIDATED) == "連結"
        assert net_sales_of(active) == 1000.0
        assert fake.calls == [("query", "db-fin"), ("update", "page-0001")]  # 追加の問い合わせ無し

        # 空の旧行は採用済みなので、単体は別の行になる
        upsert.upsert_financial_summary(fake, settings(), fin(consolidated="単体", net_sales=400.0))
        assert sorted(
            select_of(p, S.FIN_PROP_CONSOLIDATED) for p in fake.active("db-fin")
        ) == ["単体", "連結"]

    def test_the_exact_key_wins_over_an_older_legacy_row(self):
        fake = FakeNotion()
        seed(fake, fin(consolidated=None, net_sales=900.0))  # page-0001（古い）
        seed(fake, fin(consolidated="連結", net_sales=1000.0))  # page-0002
        page_id = upsert.upsert_financial_summary(
            fake, settings(), fin(consolidated="連結", net_sales=1100.0)
        )
        assert page_id == "page-0002"
        assert net_sales_of(fake._page("page-0001")) == 900.0

    def test_an_undetermined_record_does_not_overwrite_a_consolidated_row(self):
        fake = FakeNotion()
        seed(fake, fin(consolidated="連結", net_sales=1000.0))
        upsert.upsert_financial_summary(fake, settings(), fin(consolidated=None, net_sales=50.0))
        assert net_sales_of(fake._page("page-0001")) == 1000.0
        assert len(fake.active("db-fin")) == 2

    def test_the_disclosed_at_guard_still_applies_to_an_adopted_row(self):
        """#14: 採用する旧行の方が新しい開示なら、古い報告で巻き戻さない。"""
        fake = FakeNotion()
        seed(fake, fin(consolidated=None, net_sales=1100.0, disclosed_on=date(2026, 9, 1)))
        page_id = upsert.upsert_financial_summary(
            fake, settings(),
            fin(consolidated="連結", net_sales=999.0, disclosed_on=date(2026, 8, 1)),
        )
        assert page_id == "page-0001"
        assert fake.calls == [("query", "db-fin")]
        assert net_sales_of(fake._page("page-0001")) == 1100.0


class TestInterimAdoptsTheLegacySecondQuarter:
    def test_an_interim_record_adopts_the_legacy_2q_row(self):
        """移行スクリプトより先にラベル変更が出ても、同じ期を 2 行に割らない。"""
        fake = FakeNotion()
        seed(fake, fin(dtype="2Q", net_sales=900.0))
        page_id = upsert.upsert_financial_summary(
            fake, settings(), fin(dtype="中間", net_sales=1000.0)
        )
        (active,) = fake.active("db-fin")
        assert page_id == active["id"]
        assert select_of(active, S.FIN_PROP_DISCLOSURE_TYPE) == "中間"
        title = active["properties"][S.FIN_PROP_TITLE]["title"][0]["text"]["content"]
        assert title == "7203 2026/06期 中間"
        assert net_sales_of(active) == 1000.0

    def test_an_existing_interim_row_wins_over_the_legacy_2q_row(self):
        fake = FakeNotion()
        seed(fake, fin(dtype="2Q", net_sales=900.0))  # page-0001（古い）
        seed(fake, fin(dtype="中間", net_sales=1000.0))  # page-0002
        page_id = upsert.upsert_financial_summary(
            fake, settings(), fin(dtype="中間", net_sales=1100.0)
        )
        assert page_id == "page-0002"
        assert select_of(fake._page("page-0001"), S.FIN_PROP_DISCLOSURE_TYPE) == "2Q"

    def test_a_second_quarter_record_does_not_adopt_an_interim_row(self):
        fake = FakeNotion()
        seed(fake, fin(dtype="中間", net_sales=1000.0))
        upsert.upsert_financial_summary(fake, settings(), fin(dtype="2Q", net_sales=900.0))
        assert sorted(
            select_of(p, S.FIN_PROP_DISCLOSURE_TYPE) for p in fake.active("db-fin")
        ) == ["2Q", "中間"]

