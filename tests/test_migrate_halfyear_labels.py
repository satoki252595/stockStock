"""scripts/migrate_halfyear_labels.py の計画・SQL・読み取りの分割（本番へは接続しない）。"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from jp_stock_pipeline.cloud_store import schema as cloud_schema
from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.local_store.schema import SCHEMA_STATEMENTS as LOCAL_STATEMENTS
from jp_stock_pipeline.models import FinancialSummaryRecord, Provenance, Source, now_jst
from jp_stock_pipeline.notion import schema as S
from jp_stock_pipeline.notion import upsert
from jp_stock_pipeline.notion.client import QueryTruncatedError

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_halfyear_labels.py"


@pytest.fixture(scope="module")
def mig():
    spec = importlib.util.spec_from_file_location("migrate_halfyear_labels", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass が自分のモジュールを引けるように
    spec.loader.exec_module(module)
    return module


def fin_page(page_id, dtype, period_end, *, consolidated="連結", code="7203", source="EDINET"):
    return {
        "id": page_id,
        "properties": {
            S.FIN_PROP_CODE: {"type": "rich_text", "rich_text": [{"plain_text": code}]},
            S.FIN_PROP_PERIOD_END: {"type": "date", "date": {"start": period_end}},
            S.FIN_PROP_DISCLOSURE_TYPE: {"type": "select", "select": {"name": dtype}},
            S.FIN_PROP_CONSOLIDATED: {
                "type": "select", "select": {"name": consolidated} if consolidated else None,
            },
            S.PROP_SOURCE: {"type": "select", "select": {"name": source}},
        },
    }


def disc_page(page_id, title, *, doc_type="四半期報告", source="EDINET", doc_id=None):
    return {
        "id": page_id,
        "properties": {
            S.DISC_PROP_TITLE: {"type": "title", "title": [{"plain_text": title}]},
            S.DISC_PROP_DOC_TYPE: {"type": "select", "select": {"name": doc_type}},
            S.DISC_PROP_DOC_ID: {"type": "rich_text", "rich_text": [{"plain_text": doc_id or page_id}]},
            S.PROP_SOURCE: {"type": "select", "select": {"name": source}},
        },
    }


class TestPlanFinancials:
    def test_only_second_quarters_of_the_halfyear_regime_move(self, mig):
        plan = mig.plan_financials(
            [
                fin_page("a", "2Q", "2024-06-30"),
                fin_page("b", "2Q", "2025-09-30", source="TDnet"),
                fin_page("c", "2Q", "2024-06-20"),  # 2024-03-21 開始 → 四半期報告書
                fin_page("d", "2Q", "2024-05-31"),
                fin_page("e", "2Q", "2023-09-30"),
            ],
            [],
        )
        assert [u.page_id for u in plan.updates] == ["a", "b"]
        assert plan.counts["updates_by_source"] == {"EDINET": 1, "TDnet": 1}
        assert plan.counts["not_target"] == 3
        assert plan.conflicts == []

    def test_title_matches_the_writer(self, mig):
        (update,) = mig.plan_financials([fin_page("a", "2Q", "2024-09-30")], []).updates
        record = FinancialSummaryRecord(
            code="7203", fiscal_period_end=date(2024, 9, 30), disclosure_type="中間",
            provenance=Provenance(
                source=Source.EDINET, license_tag=LicenseTag.COMMERCIAL_OK,
                data_date=None, fetched_at=now_jst(),
            ),
        )
        assert update.properties == {
            S.FIN_PROP_DISCLOSURE_TYPE: upsert.select_prop("中間"),
            S.FIN_PROP_TITLE: upsert.title_prop(upsert.financial_summary_title(record)),
        }

    def test_an_existing_interim_row_of_the_same_scope_is_a_conflict(self, mig):
        plan = mig.plan_financials(
            [
                fin_page("q2-cons", "2Q", "2025-09-30", consolidated="連結"),
                fin_page("q2-solo", "2Q", "2025-09-30", consolidated="単体"),
            ],
            [fin_page("interim-cons", "中間", "2025-09-30", consolidated="連結")],
        )
        assert [u.page_id for u in plan.updates] == ["q2-solo"]
        (conflict,) = plan.conflicts
        assert conflict["second_quarter_page"] == "q2-cons"
        assert conflict["interim_pages"] == ["interim-cons"]

    def test_rerunning_after_migration_changes_nothing(self, mig):
        plan = mig.plan_financials([], [fin_page("a", "中間", "2024-06-30")])
        assert plan.updates == [] and plan.conflicts == []


class TestPlanDisclosures:
    def test_only_edinet_halfyear_reports_move(self, mig):
        plan = mig.plan_disclosures([
            disc_page("h", "半期報告書－第7期(2025/08/01－2026/07/31)"),
            disc_page("ha", "訂正半期報告書－第18期(2024/04/01－2025/03/31)"),
            disc_page("q", "四半期報告書－第106期第2四半期(2023/07/01－2023/09/30)"),
            disc_page("qa", "訂正四半期報告書－第67期第1四半期(2022/04/01－2022/06/30)"),
            disc_page("td", "半期報告書の提出に関するお知らせ", source="TDnet"),
            disc_page("other", "半期報告書－第8期", doc_type="その他"),
        ])
        assert [u.page_id for u in plan.updates] == ["h", "ha"]
        assert plan.updates[0].properties == {S.DISC_PROP_DOC_TYPE: upsert.select_prop("半期報告")}

    def test_duplicate_pages_are_all_relabelled_and_counted(self, mig):
        plan = mig.plan_disclosures([
            disc_page("p1", "半期報告書－第7期", doc_id="S100AAAA"),
            disc_page("p2", "半期報告書－第7期", doc_id="S100AAAA"),
        ])
        assert [u.page_id for u in plan.updates] == ["p1", "p2"]
        assert plan.counts["duplicate_doc_ids"] == 1
        assert plan.counts["duplicate_extra_pages"] == 1


class TestSelectOptions:
    INFO = {"properties": {"開示種別": {"type": "select", "select": {"options": [
        {"id": "a1", "name": "本決算", "color": "blue"},
        {"id": "b2", "name": "2Q", "color": "default"},
    ]}}}}

    def test_existing_options_are_sent_back_with_their_ids(self, mig):
        payload = mig.select_options_payload(self.INFO, "開示種別", ["中間"])
        assert payload == {"開示種別": {"select": {"options": [
            {"id": "a1", "name": "本決算", "color": "blue"},
            {"id": "b2", "name": "2Q", "color": "default"},
            {"name": "中間"},
        ]}}}

    def test_nothing_to_do_when_present(self, mig):
        assert mig.select_options_payload(self.INFO, "開示種別", ["2Q"]) is None

    def test_missing_property_is_an_error(self, mig):
        with pytest.raises(mig.MigrationError):
            mig.select_options_payload(self.INFO, "無い", ["中間"])


def _run_sql_flow(mig, table: str, ddl: str, *, extra: dict) -> None:
    con = sqlite3.connect(":memory:")
    con.execute(ddl)

    def insert(code, period_end, dtype, consolidated, source="EDINET", **values):
        row = {
            "code": code, "fiscal_period_end": period_end, "disclosure_type": dtype,
            "consolidated": consolidated, "source": source, "license_tag": "commercial-ok",
            "fetched_at": extra["fetched_at"], "quality": "正常", **values,
        }
        cols = ", ".join(row)
        con.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' for _ in row)})",
                    list(row.values()))

    t = extra["disclosed_at"]
    insert("A", "2024-09-30", "2Q", "連結")
    insert("A", "2024-09-30", "2Q", "単体", source="TDnet")
    insert("B", "2024-03-31", "2Q", "連結")  # 四半期報告制度の側
    # 衝突: 短信 (TDnet, 旧コード→2Q) と半期報告書 (EDINET, 新コード→中間) が同じ期に来た
    insert("C", "2025-09-30", "2Q", "連結", source="TDnet", license_tag="factual-cite",
           net_sales=100.0, forecast_eps=50.0, disclosed_at=t(1))
    insert("C", "2025-09-30", "中間", "連結", net_sales=110.0, disclosed_at=t(2))
    # 衝突で「2Q」側の方が新しい開示（旧コードが後から訂正を書いた）
    insert("E", "2025-09-30", "中間", "連結", net_sales=200.0, eps=9.0, disclosed_at=t(1))
    insert("E", "2025-09-30", "2Q", "連結", source="TDnet", net_sales=210.0, disclosed_at=t(2))
    insert("D", "2024-06-30", "本決算", "連結")
    sql = mig.interim_sql(table)
    assert con.execute(sql["count"]).fetchall() == [("EDINET", 1), ("TDnet", 3)]
    assert con.execute(sql["conflicts"]).fetchall() == [(2,)]
    assert con.execute(sql["fold"]).rowcount == 2
    assert con.execute(sql["drop_folded"]).rowcount == 2
    assert con.execute(sql["apply"]).rowcount == 2
    for step in ("fold", "drop_folded", "apply"):  # 冪等
        assert con.execute(sql[step]).rowcount == 0
    assert con.execute(sql["count"]).fetchall() == []
    assert con.execute(sql["conflicts"]).fetchall() == [(0,)]
    got = sorted(con.execute(
        f"SELECT code, fiscal_period_end, disclosure_type, consolidated FROM {table}"
    ).fetchall())
    assert got == [
        ("A", "2024-09-30", "中間", "単体"),
        ("A", "2024-09-30", "中間", "連結"),
        ("B", "2024-03-31", "2Q", "連結"),
        ("C", "2025-09-30", "中間", "連結"),  # 1 行に畳み込まれる（二重計上しない）
        ("D", "2024-06-30", "本決算", "連結"),
        ("E", "2025-09-30", "中間", "連結"),
    ]
    folded = {
        r[0]: r[1:] for r in con.execute(
            f"SELECT code, net_sales, eps, forecast_eps, source, license_tag, disclosed_at"
            f" FROM {table} WHERE code IN ('C', 'E')"
        )
    }
    # C: 中間が新しい → 値は中間を優先、中間に無い予想は短信から残す、タグは厳しい側
    assert folded["C"] == (110.0, None, 50.0, "EDINET", "factual-cite", t(2))
    # E: 2Q が新しい → 値と来歴は 2Q を優先、2Q に無い EPS は中間から残す
    assert folded["E"] == (210.0, 9.0, None, "TDnet", "commercial-ok", t(2))


class TestInterimSql:
    def test_d1(self, mig):
        ddl = next(s for s in cloud_schema.SCHEMA_STATEMENTS if "TABLE IF NOT EXISTS jss_financials" in s)
        _run_sql_flow(
            mig, "jss_financials", ddl, extra={"fetched_at": 1, "disclosed_at": lambda d: d},
        )

    def test_local(self, mig):
        ddl = next(s for s in LOCAL_STATEMENTS if "TABLE IF NOT EXISTS financials" in s)
        _run_sql_flow(
            mig, "financials", ddl.replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP"),
            extra={
                "fetched_at": "2026-09-13T00:00:00+09:00",
                "disclosed_at": lambda d: f"2025-11-0{d}T15:00:00+09:00",
            },
        )

    def test_unknown_table_is_rejected(self, mig):
        with pytest.raises(mig.MigrationError):
            mig.interim_sql("financials; DROP TABLE x")


class TestScanWindows:
    def test_truncated_windows_are_split_in_half(self, mig):
        calls = []

        class _Client:
            def query_database(self, db_id, *, filter, strict):
                assert strict is True
                start = date.fromisoformat(filter["and"][-2]["date"]["on_or_after"])
                end = date.fromisoformat(filter["and"][-1]["date"]["before"])
                calls.append((start, end))
                if (end - start).days > 100:
                    raise QueryTruncatedError("10,000 件で打ち切り")
                return [{"id": start.isoformat()}]

        pages = mig.scan_windows(_Client(), "db", [], "決算期末", [(date(2025, 1, 1), date(2026, 1, 1))])
        covered = sorted((s, e) for s, e in calls if (e - s).days <= 100)
        assert covered[0][0] == date(2025, 1, 1) and covered[-1][1] == date(2026, 1, 1)
        assert all(a[1] == b[0] for a, b in zip(covered, covered[1:], strict=False))
        assert len(pages) == len(covered)

    def test_a_single_day_over_the_limit_is_an_error(self, mig):
        class _Client:
            def query_database(self, *a, **k):
                raise QueryTruncatedError("10,000 件で打ち切り")

        day = date(2025, 1, 1)
        with pytest.raises(mig.MigrationError):
            mig.scan_windows(_Client(), "db", [], "開示日時", [(day, day + timedelta(days=1))])

    def test_year_windows(self, mig):
        assert mig.year_windows(date(2024, 6, 30), date(2026, 1, 1)) == [
            (date(2024, 6, 30), date(2025, 1, 1)),
            (date(2025, 1, 1), date(2026, 1, 1)),
        ]


class _FinClient:
    """③ の読み取りだけを持つフェイク。存在しない選択肢のフィルタは Notion と同じく失敗させる。"""

    def __init__(self, options, pages):
        self.options, self.pages, self.updates = options, pages, []

    def retrieve_database(self, db_id):
        return {"properties": {S.FIN_PROP_DISCLOSURE_TYPE: {"type": "select", "select": {
            "options": [{"id": n, "name": n} for n in self.options]}}}}

    def query_database(self, db_id, *, filter, strict):
        conds = filter["and"] if "and" in filter else [filter]
        for cond in conds:
            name = (cond.get("select") or {}).get("equals")
            if name is not None and name not in self.options:
                raise RuntimeError(f'select option "{name}" not found')
        wanted = next((c["select"]["equals"] for c in conds
                       if c.get("property") == S.FIN_PROP_DISCLOSURE_TYPE), None)
        if wanted is None:
            return []
        start = conds[-2]["date"]["on_or_after"]
        end = conds[-1]["date"]["before"]
        return [p for p in self.pages
                if p["properties"][S.FIN_PROP_DISCLOSURE_TYPE]["select"]["name"] == wanted
                and start <= p["properties"][S.FIN_PROP_PERIOD_END]["date"]["start"] < end]

    def update_page(self, page_id, properties):
        self.updates.append(page_id)


class TestRunFinancialsBeforeOptionsExist:
    SETTINGS = type("S", (), {"notion_rps": 2.5, "db_id": staticmethod(lambda key: "db-fin")})()

    def test_dry_run_works_without_the_interim_option(self, mig):
        client = _FinClient(["本決算", "2Q"], [fin_page("a", "2Q", "2025-09-30")])
        report = mig.run_financials(client, self.SETTINGS, apply=False, limit=None)
        assert report["counts"]["updates"] == 1
        assert report["interim_option_exists"] is False
        assert client.updates == []

    def test_apply_refuses_until_the_option_exists(self, mig):
        client = _FinClient(["本決算", "2Q"], [fin_page("a", "2Q", "2025-09-30")])
        with pytest.raises(mig.MigrationError, match="options"):
            mig.run_financials(client, self.SETTINGS, apply=True, limit=None)
        assert client.updates == []

