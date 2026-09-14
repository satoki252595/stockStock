"""鮮度 SLO とジョブ結果記録のテスト。

「壊れても音が鳴らない」が本システム最大の欠陥だった。実測:
  EDINET 11 営業日連続 processed=0 で「成功」/ JPX 33 日停止 / 優待 81.5 日停止
  jss_job_runs も jss_dataset_freshness も 0 行（writer 不在）
ここが機能しなくなったら気づけるようにする。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta

import pytest

from _doubles import SqliteD1

from jp_stock_pipeline.cloud_store import datasets, ops, slo
from jp_stock_pipeline.jobs import freshness_probe, ops_check, runner

# 移行元 kabulab-cf が所有する既存表。stockStock の schema.py は jss_* しか作らない
# ので、観測 SQL を流すためにテスト側で最小の DDL を置く。**マニフェストの SQL が
# 触る列だけ**を宣言している。
#
# 限界: ここは本番の DDL のコピーではないので、本番側の列名変更や列名のタイプミスは
# このテストでは検出できない（検出できるのは本番で実クエリを投げたときだけ）。
# だから PR では「マージ後に freshness_probe を手動実行して 7 行入ることを確認する」
# を必須手順にしている。
_LEGACY_DDL: tuple[str, ...] = (
    "CREATE TABLE core_stock_financials"
    " (stock_id INTEGER, data_date TEXT, fetched_at INTEGER)",
    # 取得時刻の列は宣言しない。設計書にも既存コードにも出てこないので、
    # マニフェストがそれを参照していたらここで `no such column` にして落とす。
    "CREATE TABLE ir_disclosures (tdnet_id TEXT, pubdate INTEGER)",
    "CREATE TABLE core_stocks (id INTEGER PRIMARY KEY, code TEXT, updated_at INTEGER)",
    "CREATE TABLE yutai_benefits"
    " (id INTEGER PRIMARY KEY, stock_id INTEGER, updated_at INTEGER)",
)


def _FakeStore() -> SqliteD1:
    """D1Store の代わりにローカル sqlite へ本物の SQL を流す。"""
    from jp_stock_pipeline.cloud_store.schema import SCHEMA_STATEMENTS

    return SqliteD1(ddl=(*SCHEMA_STATEMENTS, *_LEGACY_DDL))


_D1_ENV = {
    # run_job は dry-run でない限り NOTION_TOKEN を要求する（判定ジョブでも通る経路）
    "NOTION_TOKEN": "dummy-token",
    "CF_ACCOUNT_ID": "acct",
    "CF_API_TOKEN": "token",
    "CF_D1_DATABASE_ID": "db",
}


def _wire(monkeypatch, store: _FakeStore, *modules) -> None:
    """run_job 経由で走らせるための配線。本物の HTTP を一切出させない。"""
    from jp_stock_pipeline.cloud_store import d1 as d1_module

    def factory(settings, *, writer, database_id=None):
        return store

    for module in modules:
        monkeypatch.setattr(module, "D1Store", factory)
    # runner は関数内で d1 を import するのでモジュール属性を差し替える
    monkeypatch.setattr(d1_module, "D1Store", factory)
    monkeypatch.setattr(runner, "connect_local_store", lambda *a, **k: None)


def _jst_today() -> date:
    """JST の当日。UTC の暦日を使うと JST 夜間にテストが揺れる。"""
    return datetime.now(slo.JST).date()


NOW = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)

# 観測ジョブが記録するデータセット（判定するもの + 判定しないが観測は続けるもの）。
_OBSERVED = set(slo.SLO_BY_DATASET) | set(slo.NOT_REFRESHED)


def _epoch_days_ago(days: float) -> int:
    return int((NOW - timedelta(days=days)).timestamp())


class TestFreshnessSlo:
    def test_全データセットに閾値がある(self) -> None:
        """閾値が無いと「33日古い」が異常か判定できない。

        `yutai_benefits` は「更新しない」と決まり閾値ごと `NOT_REFRESHED` へ移した
        ので、`SLOS` 単独の件数は 6 になった。守りたいのは「観測する全データセットが
        閾値を持つか、判定しない理由を持つか」なので、和で 7 件を下回らないことを見る。
        """
        assert len(slo.SLOS) + len(slo.NOT_REFRESHED) >= 7
        for s in slo.SLOS:
            assert s.green_hours < s.yellow_hours, s.dataset
            assert s.note, s.dataset

    def test_未定義のデータセットは緑にしない(self) -> None:
        """知らないものを「問題なし」に倒さない。"""
        assert slo.judge("unknown_dataset", _epoch_days_ago(0), now=NOW) == "unknown"

    def test_記録が無ければ緑にしない(self) -> None:
        assert slo.judge("d1_core_stock_financials", None, now=NOW) == "unknown"
        assert slo.judge("d1_core_stock_financials", 0, now=NOW) == "unknown"

    @pytest.mark.parametrize(
        ("dataset", "days", "expected"),
        [
            # 実測値そのもの
            ("d1_core_stock_financials", 0.1, "green"),      # 2.4h
            ("jsf_supply", 0.5, "green"),
            ("core_stocks", 33.0, "green"),       # 33.0日 = まだ緑（あと7日で黄）
            ("core_stocks", 41.0, "yellow"),
            ("core_stocks", 46.0, "red"),
            ("edinet_documents", 0.5, "green"),
            ("edinet_documents", 1.5, "yellow"),  # 36h
            ("edinet_documents", 3.0, "red"),
        ],
    )
    def test_実測値の判定(self, dataset: str, days: float, expected: str) -> None:
        assert slo.judge(dataset, _epoch_days_ago(days), now=NOW) == expected

    def test_境界ちょうどは緑(self) -> None:
        s = slo.SLO_BY_DATASET["d1_core_stock_financials"]
        assert s.judge(s.green_hours) == "green"
        assert s.judge(s.green_hours + 0.01) == "yellow"
        assert s.judge(s.yellow_hours) == "yellow"
        assert s.judge(s.yellow_hours + 0.01) == "red"


class TestRecordJobRun:
    def test_実行結果を残す(self) -> None:
        store = _FakeStore()
        ops.record_job_run(
            store, job_name="edinet_daily", status="成功", processed=0, failed=0,
            failed_codes=[], run_url="https://example/1", duration_secs=70.0,
            finished_at=int(datetime.now(UTC).timestamp()),
        )
        rows = store.query("SELECT * FROM jss_job_runs")
        assert len(rows) == 1
        assert rows[0]["job_name"] == "edinet_daily"
        assert rows[0]["processed"] == 0

    def test_失敗コードは上限で切る(self) -> None:
        store = _FakeStore()
        codes = [f"{i:04d}" for i in range(200)]
        ops.record_job_run(
            store, job_name="j", status="一部失敗", processed=1, failed=200,
            failed_codes=codes, run_url=None, duration_secs=None,
            finished_at=int(datetime.now(UTC).timestamp()),
        )
        saved = json.loads(store.query("SELECT failed_codes FROM jss_job_runs")[0]["failed_codes"])
        assert len(saved) == ops.MAX_FAILED_CODES

    def test_処理ゼロの成功も残る(self) -> None:
        """EDINET が 11 営業日「成功 processed=0」だったのを後から見つけられるように。"""
        store = _FakeStore()
        now = int(datetime.now(UTC).timestamp())
        for day in range(11):
            ops.record_job_run(
                store, job_name="edinet_daily", status="成功", processed=0, failed=0,
                failed_codes=[], run_url=None, duration_secs=70.0,
                finished_at=now - day * 86400,
            )
        rows = store.query(
            "SELECT COUNT(*) AS n FROM jss_job_runs"
            " WHERE job_name='edinet_daily' AND status='成功' AND processed=0"
        )
        assert rows[0]["n"] == 11

    def test_90日より古い履歴は書き込みのたび剪定する(self) -> None:
        """剪定が無いと空振り検知の ROW_NUMBER が無限に育つ (L-17)。"""
        store = _FakeStore()
        old = int((datetime.now(UTC) - timedelta(days=100)).timestamp())
        store.con.execute(
            "INSERT INTO jss_job_runs (job_name, status, processed, failed,"
            " failed_codes, run_url, duration_secs, finished_at)"
            " VALUES ('old_job', '成功', 0, 0, NULL, NULL, NULL, ?)",
            (old,),
        )
        store.con.commit()
        ops.record_job_run(
            store, job_name="new_job", status="成功", processed=1, failed=0,
            failed_codes=[], run_url=None, duration_secs=1.0,
            finished_at=int(datetime.now(UTC).timestamp()),
        )
        rows = store.query("SELECT job_name FROM jss_job_runs")
        assert [r["job_name"] for r in rows] == ["new_job"]

    def test_記録に失敗してもジョブを落とさない(self) -> None:
        class Broken:
            def query(self, sql, params=None):
                from jp_stock_pipeline.cloud_store.d1 import D1Error

                raise D1Error("boom")

        assert ops.safe_record_job_run(Broken(), job_name="j", status="成功", processed=1,
                                       failed=0, failed_codes=[], run_url=None,
                                       duration_secs=None, finished_at=1) is False

    def test_D1_未設定なら記録しないが落ちない(self) -> None:
        assert ops.safe_record_job_run(None, job_name="j", status="成功", processed=1,
                                       failed=0, failed_codes=[], run_url=None,
                                       duration_secs=None, finished_at=1) is False


class TestRecordFreshness:
    def test_1データセット1行で上書きする(self) -> None:
        store = _FakeStore()
        for n, ts in ((10, 100), (20, 200)):
            ops.record_freshness(
                store, dataset="d1_core_stock_financials", store_name="D1", location="core_stock_financials",
                writer="kabulab-cf daily.ts", latest_data_date="2026-09-11",
                row_or_object_count=n, bytes_=None, license_tag="personal-only", updated_at=ts,
            )
        rows = store.query("SELECT * FROM jss_dataset_freshness")
        assert len(rows) == 1
        assert rows[0]["row_or_object_count"] == 20
        assert rows[0]["updated_at"] == 200

    def test_date型を受け付ける(self) -> None:
        from datetime import date

        store = _FakeStore()
        ops.record_freshness(
            store, dataset="d", store_name="D1", location="t", writer="w",
            latest_data_date=date(2026, 9, 11), row_or_object_count=1, bytes_=None,
            license_tag=None, updated_at=1,
        )
        assert store.query("SELECT latest_data_date FROM jss_dataset_freshness")[0][
            "latest_data_date"
        ] == "2026-09-11"


class TestDatasetManifest:
    def test_SLOとマニフェストのデータセット集合が一致する(self) -> None:
        """片方だけ増えると「閾値はあるが測っていない」「測っているが判定されない」

        のどちらかが静かに生まれる。等号で縛る。

        判定しない（`NOT_REFRESHED`）データセットも観測は続けるので、マニフェストは
        「判定する」と「判定しないと理由つきで宣言した」の**和**と一致させる。
        両方に入る名前は import 時に `validate_declarations` が弾くので、和は
        互いに素である。
        """
        assert not set(slo.SLO_BY_DATASET) & set(slo.NOT_REFRESHED)
        assert set(datasets.DATASET_SOURCE_BY_NAME) == (
            set(slo.SLO_BY_DATASET) | set(slo.NOT_REFRESHED)
        )

    def test_SQLは1文の集約でUNIONを使わない(self) -> None:
        """D1 の compound SELECT 上限は 5。UNION を許すと構造的に抵触する。"""
        for src in datasets.DATASET_SOURCES:
            upper = src.sql.upper()
            # n は件数 (COUNT) か存在 (CASE WHEN MAX..IS NULL) のどちらか。
            # 大きい表は存在判定にしないと索引シークが全走査に落ちる (L-17)。
            assert "COUNT(" in upper or "CASE WHEN MAX(" in upper, src.dataset
            assert "MAX(" in upper, src.dataset
            assert "SELECT *" not in upper, src.dataset
            for forbidden in ("UNION", "INTERSECT", "EXCEPT"):
                assert forbidden not in upper, f"{src.dataset}: {forbidden}"

    def test_ライセンスタグが全件埋まっている(self) -> None:
        """1件でも None があると公開面のフィルタが判断できない。"""
        from jp_stock_pipeline.licensing import LicenseTag

        valid = {t.value for t in LicenseTag}
        for src in datasets.DATASET_SOURCES:
            assert src.license_tag in valid, src.dataset

    def test_混在する表は最も厳しいタグへ倒す(self) -> None:
        """core_stocks を commercial-ok にすると JPX 由来の断面メタが公開 API に出る。"""
        assert datasets.DATASET_SOURCE_BY_NAME["core_stocks"].license_tag == "personal-only"

    def test_uniform表のタグは地図と一致する(self) -> None:
        """二重宣言にしない（L-37）。導出元が uniform で無くなったら落ちる。"""
        from jp_stock_pipeline.cloud_store import governance as G

        checked = set()
        for src in datasets.DATASET_SOURCES:
            spec = G.TABLE_LICENSE.get(src.location)
            if spec is None or spec.kind is not G.TableKind.UNIFORM:
                continue
            checked.add(src.dataset)
            assert spec.tag is not None, src.dataset
            assert src.license_tag == spec.tag.value, (
                f"{src.dataset}: マニフェスト {src.license_tag} /"
                f" 地図 {spec.tag.value}"
            )
        # 空振り防止：今日 uniform 観測はこの 3 件。
        assert checked == {
            "d1_core_stock_financials", "tdnet_disclosures", "yutai_benefits",
        }, checked

    def test_uniformでない表は導出を拒否する(self) -> None:
        """行タグ・列地図・絞り込み観測に `_uniform_tag` を使うと落ちる。"""
        with pytest.raises(ValueError, match="uniform ではない"):
            datasets._uniform_tag("jss_raw_files")
        with pytest.raises(ValueError, match="uniform ではない"):
            datasets._uniform_tag("core_stocks")

    def test_観測SQLは読み取りだけ(self) -> None:
        """観測ジョブは移行元 (kabulab-cf) の本番 DB へのハンドルも持つ。

        マニフェストに書き込み文が混ざると、そこが移行元の本番データを壊す唯一の
        経路になる。SELECT 以外は構造的に禁止する。
        """
        # 単語境界で見る（`updated_at` の部分一致で誤検知しないため）。
        forbidden = re.compile(
            r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|CREATE|ATTACH|PRAGMA|VACUUM)\b"
        )
        for src in datasets.DATASET_SOURCES:
            upper = src.sql.upper()
            assert upper.lstrip().startswith("SELECT"), src.dataset
            assert ";" not in src.sql, f"{src.dataset}: 複文にしない"
            hit = forbidden.search(upper)
            assert hit is None, f"{src.dataset}: {hit.group(0) if hit else ''}"

    def test_epoch列の日付化はJSTへ寄せる(self) -> None:
        """UTC のままだと他データセットの JST 営業日文字列と 1 日ずれる。"""
        sql = datasets.DATASET_SOURCE_BY_NAME["tdnet_disclosures"].sql
        assert "'unixepoch','+9 hours'" in sql


class TestJudgeObservation:
    def test_測った結果ゼロ件は赤(self) -> None:
        """0 件は「分からない」ではない。jss_financials が実際にこれ。"""
        assert slo.judge_observation(
            "financials", latest_data_date=None, source_epoch=None, row_count=0, now=NOW
        ) == "red"

    def test_測れていないならunknown(self) -> None:
        assert slo.judge_observation(
            "d1_core_stock_financials", latest_data_date=None, source_epoch=None,
            row_count=None, now=NOW,
        ) == "unknown"

    def test_取得時刻が新しくてもデータ基準日が古ければ赤(self) -> None:
        """偽の緑の回帰テスト。

        jss_supply_latest は data_date=2026-09-10 なのに fetched_at は 2026-09-11。
        supply_daily は fetched_at を全行 now_jst() で塗り直すので、取得時刻基準では
        日証金が同じスナップショットを返し続けても永遠に緑になる。
        """
        fresh_epoch = _epoch_days_ago(0.2)
        assert slo.judge_observation(
            "jsf_supply", latest_data_date="2026-08-20", source_epoch=fresh_epoch,
            row_count=4351, now=NOW,
        ) == "red"
        # 取得時刻だけを見る旧判定だと緑になってしまうことを明示しておく
        assert slo.judge("jsf_supply", fresh_epoch, now=NOW) == "green"

    def test_基準日の列が無い表は取得時刻で測る(self) -> None:
        """core_stocks は日付列が無いのでこれしか手がない。"""
        assert slo.judge_observation(
            "core_stocks", latest_data_date=None, source_epoch=_epoch_days_ago(33.0),
            row_count=4445, now=NOW,
        ) == "green"
        assert slo.judge_observation(
            "core_stocks", latest_data_date=None, source_epoch=_epoch_days_ago(46.0),
            row_count=4445, now=NOW,
        ) == "red"

    def test_読めない基準日は取得時刻へ落ちる(self) -> None:
        assert slo.judge_observation(
            "d1_core_stock_financials", latest_data_date="不明", source_epoch=_epoch_days_ago(0.1),
            row_count=3764, now=NOW,
        ) == "green"


class TestBusinessDayAging:
    """土日月に日次データセットが赤にならないこと（週3日鳴る通知を作らない）。"""

    # 2026-09-11 は金曜。観測は 23:30 JST（cron 14:30 UTC）に走る。
    FRIDAY = "2026-09-11"

    @pytest.mark.parametrize(
        ("label", "now_jst_day"),
        [("土", 12), ("日", 13), ("月", 14)],
    )
    @pytest.mark.parametrize("dataset", ["d1_core_stock_financials", "tdnet_disclosures", "edinet_documents"])
    def test_金曜の基準日は土日月でも赤にならない(
        self, dataset: str, label: str, now_jst_day: int
    ) -> None:
        now = datetime(2026, 9, now_jst_day, 23, 30, tzinfo=slo.JST)
        verdict = slo.judge_observation(
            dataset, latest_data_date=self.FRIDAY, source_epoch=None,
            row_count=100, now=now,
        )
        assert verdict in ("green", "yellow"), f"{dataset} {label}曜: {verdict}"

    def test_土日は緑のまま(self) -> None:
        for day in (12, 13):
            now = datetime(2026, 9, day, 23, 30, tzinfo=slo.JST)
            assert slo.judge_observation(
                "d1_core_stock_financials", latest_data_date=self.FRIDAY, source_epoch=None,
                row_count=100, now=now,
            ) == "green"

    def test_暦時間のままなら日曜に赤だった(self) -> None:
        """営業日加齢が無いと何が起きていたかを残す（この値が回帰の根拠）。"""
        sunday = datetime(2026, 9, 13, 23, 30, tzinfo=slo.JST)
        friday_midnight = datetime(2026, 9, 11, 0, 0, tzinfo=slo.JST)
        calendar = slo.elapsed_hours(friday_midnight, sunday, business_days=False)
        assert calendar > slo.SLO_BY_DATASET["d1_core_stock_financials"].yellow_hours
        assert slo.SLO_BY_DATASET["d1_core_stock_financials"].judge(calendar) == "red"

    def test_公表遅延のあるデータセットも平常時は緑(self) -> None:
        """日証金の貸借残は翌営業日公表なので data_date は常に T-1。

        lag_days が無いと jsf_supply は平常運転でも恒久的に黄になる。
        """
        friday_night = datetime(2026, 9, 11, 23, 30, tzinfo=slo.JST)
        assert slo.judge_observation(
            "jsf_supply", latest_data_date="2026-09-10", source_epoch=None,
            row_count=4351, now=friday_night,
        ) == "green"

    def test_営業日が1日抜けると黄_2日で赤(self) -> None:
        """検知力が落ちていないこと（緑に寄せすぎない）。"""
        # 火曜 23:30 に基準日が金曜 = 月・火の 2 営業日欠落
        tuesday = datetime(2026, 9, 15, 23, 30, tzinfo=slo.JST)
        assert slo.judge_observation(
            "d1_core_stock_financials", latest_data_date="2026-09-11", source_epoch=None,
            row_count=100, now=tuesday,
        ) == "red"
        # 月曜 23:30 なら 1 営業日欠落 = 黄
        monday = datetime(2026, 9, 14, 23, 30, tzinfo=slo.JST)
        assert slo.judge_observation(
            "d1_core_stock_financials", latest_data_date="2026-09-11", source_epoch=None,
            row_count=100, now=monday,
        ) == "yellow"


class TestAcceptedRed:
    def test_writerができたデータセットは受容から外す(self) -> None:
        """`financials` の writer（`cloud_store/financials.py`）が出来た。

        受容したままだと「実装済みの writer が動いていない」が受容済みとして
        沈黙する。受容は「今は直せない」ものに限る。
        """
        assert "financials" not in slo.ACCEPTED_RED
        assert "financials" in slo.SLO_BY_DATASET

    def test_受容理由が必須(self) -> None:
        """理由の無い受容は消音と区別できず、誰も外せなくなる。"""
        assert set(slo.ACCEPTED_RED) <= set(slo.SLO_BY_DATASET)
        for dataset, reason in slo.ACCEPTED_RED.items():
            assert reason.strip(), dataset
            assert len(reason) > 20, dataset


class TestNotRefreshed:
    """「そもそも更新しない」データセット。判定から外すのは加齢だけ。"""

    def test_優待は受容済みの赤ではなく更新しないデータセット(self) -> None:
        """2026-09-13 のユーザ判断。いつか直す赤（ACCEPTED_RED）とは区分が違う。"""
        assert "yutai_benefits" in slo.NOT_REFRESHED
        assert "yutai_benefits" not in slo.ACCEPTED_RED
        assert "yutai_benefits" not in slo.SLO_BY_DATASET, "閾値を残すと嘘の閾値になる"

    def test_理由が必須(self) -> None:
        for dataset, reason in slo.NOT_REFRESHED.items():
            assert len(reason.strip()) > 20, dataset
        with pytest.raises(ValueError, match="理由が無い"):
            slo.validate_declarations(slo.SLO_BY_DATASET, {}, {"yutai_benefits": "  "})

    def test_受容済みの赤と同時に宣言するとエラー(self) -> None:
        with pytest.raises(ValueError, match="両方に入っている"):
            slo.validate_declarations(
                slo.SLO_BY_DATASET,
                {"core_stocks": "直すまで受容する理由"},
                {"core_stocks": "更新しない理由"},
            )

    def test_閾値があるのに判定対象外にするとエラー(self) -> None:
        with pytest.raises(ValueError, match="SLOS に閾値がある"):
            slo.validate_declarations(
                slo.SLO_BY_DATASET, {}, {"core_stocks": "更新しない理由"}
            )

    def test_矛盾した宣言では_import_時に落ちる(self, monkeypatch) -> None:
        """`validate_declarations` を関数として試すだけでは、import 時の呼び出しを
        消しても緑のままになる（矛盾した宣言で ops_check が黙って起動する）。

        slo.py の本文を別名のモジュールとして、宣言だけ矛盾させて実行する。
        """
        import sys
        import types
        from pathlib import Path

        path = Path(slo.__file__)
        src = path.read_text(encoding="utf-8")
        original = "ACCEPTED_RED: dict[str, str] = {}"
        assert original in src, "ACCEPTED_RED の定義行が変わったらこのテストも直す"
        src = src.replace(
            original,
            'ACCEPTED_RED: dict[str, str] = {"yutai_benefits": "矛盾させるための受容理由"}',
        )
        name = "jp_stock_pipeline.cloud_store._slo_contradiction_probe"
        module = types.ModuleType(name)
        module.__package__ = "jp_stock_pipeline.cloud_store"
        module.__file__ = str(path)
        monkeypatch.setitem(sys.modules, name, module)
        with pytest.raises(ValueError, match="両方に入っている"):
            exec(compile(src, str(path), "exec"), module.__dict__)

    def test_現在の宣言は矛盾していない(self) -> None:
        slo.validate_declarations(slo.SLO_BY_DATASET, slo.ACCEPTED_RED, slo.NOT_REFRESHED)

    def test_何日止まっていても判定対象外(self) -> None:
        """81.96 日でも 800 日でも赤にも緑にもしない。"""
        for days in (81.96, 800.0):
            assert slo.judge_observation(
                "yutai_benefits", latest_data_date=None,
                source_epoch=_epoch_days_ago(days), row_count=8314, now=NOW,
            ) == slo.VERDICT_NOT_REFRESHED
        # 緑でも unknown でもない別の値であること（緑は「新鮮」と読まれ、
        # unknown は ops_check が違反として毎日鳴らす）。
        assert slo.VERDICT_NOT_REFRESHED not in ("green", "yellow", "red", "unknown")

    def test_判定対象外でも0行は赤_測れなければunknown(self) -> None:
        """更新しないことと、再取得不能な資産が消えてよいことは違う。"""
        assert slo.judge_observation(
            "yutai_benefits", latest_data_date=None, source_epoch=None,
            row_count=0, now=NOW,
        ) == "red"
        assert slo.judge_observation(
            "yutai_benefits", latest_data_date=None, source_epoch=None,
            row_count=None, now=NOW,
        ) == "unknown"

    def test_加齢はログ用に暦時間で返す(self) -> None:
        age = slo.observation_age_hours(
            "yutai_benefits", latest_data_date=None,
            source_epoch=_epoch_days_ago(10.0), now=NOW,
        )
        assert age == pytest.approx(240.0)

    def test_定義に無いデータセットは判定対象外とも扱わない(self) -> None:
        """未定義を「判定しない」へ倒すと、名前のタイプミスが黙って消える。"""
        assert slo.judge_observation(
            "yutai_benefit", latest_data_date=None, source_epoch=_epoch_days_ago(0),
            row_count=1, now=NOW,
        ) == "unknown"
        assert slo.observation_age_hours(
            "yutai_benefit", latest_data_date=None, source_epoch=_epoch_days_ago(0), now=NOW,
        ) is None


class TestFreshnessProbe:
    """観測ジョブ。記録だけを行い、判定も通知もしない。"""

    def _ctx(self, *, dry_run: bool = False) -> runner.JobContext:
        import argparse

        from jp_stock_pipeline.config import load_settings
        from jp_stock_pipeline.notion.client import NotionClient

        settings = load_settings(dry_run=dry_run or None, env=dict(_D1_ENV))
        return runner.JobContext(
            settings=settings,
            client=NotionClient(None, rps=1000.0, dry_run=True),
            args=argparse.Namespace(dry_run=dry_run),
        )

    def _seed_all(self, store: _FakeStore, *, prices_fetched_at: int) -> None:
        """7 データセットぶんの実表に 1 行ずつ置く。"""
        today = _jst_today().isoformat()
        store.query(
            "INSERT INTO core_stock_financials (stock_id, data_date, fetched_at)"
            " VALUES (1, ?, ?)",
            [today, prices_fetched_at],
        )
        store.query(
            "INSERT INTO ir_disclosures (tdnet_id, pubdate) VALUES ('t', ?)",
            [prices_fetched_at],
        )
        store.query(
            "INSERT INTO core_stocks (code, updated_at) VALUES ('7203', ?)",
            [prices_fetched_at],
        )
        store.query(
            "INSERT INTO yutai_benefits (stock_id, updated_at) VALUES (1, ?)",
            [prices_fetched_at],
        )
        store.query(
            "INSERT INTO jss_supply_latest"
            " (code, data_type, data_date, r2_key, license_tag, fetched_at, quality)"
            " VALUES ('7203', 'jsf_zandaka', ?, 'k', 'personal-only', ?, '正常')",
            [today, prices_fetched_at],
        )
        store.query(
            "INSERT INTO jss_raw_files"
            " (sha256, r2_bucket, r2_key, source, datatype, scope, data_date, ext,"
            "  size_bytes, license_tag, convert_status, first_fetched_at, last_fetched_at)"
            " VALUES ('s', 'b', 'k', 'EDINET', 'xbrl', 'x', ?, 'zip', 1,"
            "  'commercial-ok', '完了', ?, ?)",
            [today, prices_fetched_at, prices_fetched_at],
        )
        # financials (jss_financials) は本番同様 0 行のままにしておく

    def test_記録するupdated_atは実表のepochで記録時刻ではない(self, monkeypatch) -> None:
        """偽の緑の回帰テスト。

        ここに now を入れると、実表が凍結していても翌日から恒久的に緑になる。
        """
        store = _FakeStore()
        frozen = _epoch_days_ago(3.0)
        self._seed_all(store, prices_fetched_at=frozen)
        _wire(monkeypatch, store, freshness_probe)
        ctx = self._ctx()
        freshness_probe.execute(ctx)
        rows = {
            r["dataset"]: r
            for r in store.query("SELECT dataset, updated_at FROM jss_dataset_freshness")
        }
        assert rows["d1_core_stock_financials"]["updated_at"] == frozen

    def test_空表はredとして判定される(self, monkeypatch) -> None:
        """jss_financials は 0 行。測れた上でのゼロ件を unknown に倒さない。"""
        store = _FakeStore()
        self._seed_all(store, prices_fetched_at=_epoch_days_ago(0.1))
        _wire(monkeypatch, store, freshness_probe)
        ctx = self._ctx()
        freshness_probe.execute(ctx)
        assert ctx.failed == 0, "0 行の表は記録できる（記録できないとジョブが毎日赤になる）"
        row = store.query(
            "SELECT row_or_object_count AS n, updated_at FROM jss_dataset_freshness"
            " WHERE dataset = 'financials'"
        )[0]
        assert row["n"] == 0
        assert slo.judge_observation(
            "financials", latest_data_date=None, source_epoch=row["updated_at"],
            row_count=row["n"],
        ) == "red"

    def test_7件すべて記録される(self, monkeypatch) -> None:
        store = _FakeStore()
        self._seed_all(store, prices_fetched_at=_epoch_days_ago(0.1))
        _wire(monkeypatch, store, freshness_probe)
        ctx = self._ctx()
        freshness_probe.execute(ctx)
        assert ctx.failed == 0
        rows = store.query("SELECT dataset, license_tag FROM jss_dataset_freshness")
        assert {r["dataset"] for r in rows} == _OBSERVED
        assert all(r["license_tag"] for r in rows)

    def test_1表が壊れても他6件は記録されジョブは失敗する(self, monkeypatch) -> None:
        """runner は failed>0 & processed>0 を exit 0 にするので、素直に書くと

        表名のタイプミスで 1 件落ちても Issue が立たず、凍結した行が SLO を
        超えるまで（優待なら最長 50 日）気づけない。
        """
        store = _FakeStore()
        self._seed_all(store, prices_fetched_at=_epoch_days_ago(0.1))
        store.query("DROP TABLE core_stocks")  # 1 表だけ測れない状態にする
        _wire(monkeypatch, store, freshness_probe)
        code = freshness_probe.main([], env=dict(_D1_ENV))
        recorded = {
            r["dataset"]
            for r in store.query("SELECT dataset FROM jss_dataset_freshness")
        }
        assert recorded == _OBSERVED - {"core_stocks"}
        assert code == 1, "1 件でも測れなければジョブ全体を失敗にする"

    def test_dry_runは1文も書き込まない(self, monkeypatch) -> None:
        """run_job 経由で確認する。fake store を直接注入するだけでは

        「ctx.cloud が dry-run で None」の欠陥を踏まず緑になってしまう。
        """
        store = _FakeStore()
        self._seed_all(store, prices_fetched_at=_epoch_days_ago(0.1))
        seeded_writes = len(store.write_sql)
        _wire(monkeypatch, store, freshness_probe)
        code = freshness_probe.main(["--dry-run"], env=dict(_D1_ENV))
        assert code == 0, "dry-run は完走しなければならない"
        assert len(store.write_sql) == seeded_writes, store.write_sql[seeded_writes:]
        assert store.query("SELECT COUNT(*) AS n FROM jss_dataset_freshness")[0]["n"] == 0


class TestOpsCheck:
    """判定ジョブ。何も書かず、異常だけを報告する。"""

    def _seed_freshness(self, store: _FakeStore, *, overrides=None) -> None:
        """全 7 データセットを「判定対象外 1 件 + 緑 6 件」で埋める。

        `yutai_benefits` は「更新しない」と決まり `slo.NOT_REFRESHED` へ移ったので、
        82 日止まっていても判定されない（以前はここが「宣言済みの赤」だった）。

        `financials` は writer（`cloud_store/financials.py`）が出来たので
        `slo.ACCEPTED_RED` から外れた。したがって 0 行はもう「宣言済みの赤」では
        なく**本物の赤**であり、この土台では緑（行がある状態）に置く。
        0 行が exit 1 になることは
        `test_financialsの0行は宣言外の赤としてexit1` が別に固定する。
        """
        today = _jst_today().isoformat()
        recent = int(datetime.now(UTC).timestamp()) - 3600
        rows = {
            "d1_core_stock_financials": (today, 3764, recent),
            "tdnet_disclosures": (today, 37338, recent),
            "edinet_documents": (today, 100, recent),
            "jsf_supply": (today, 4351, recent),
            "core_stocks": (None, 4445, recent),
            "financials": (today, 12000, recent),
            "yutai_benefits": (
                None, 8314, int((datetime.now(UTC) - timedelta(days=82)).timestamp()),
            ),  # 判定対象外（更新しないデータセット）
        }
        rows.update(overrides or {})
        for dataset, (latest, n, epoch) in rows.items():
            ops.record_freshness(
                store, dataset=dataset, store_name="D1", location=dataset,
                writer="w", latest_data_date=latest, row_or_object_count=n,
                bytes_=None, license_tag="personal-only", updated_at=epoch or 0,
            )

    def _seed_probe_ok(self, store: _FakeStore) -> None:
        ops.record_job_run(
            store, job_name="freshness_probe", status=runner.STATUS_SUCCESS,
            processed=7, failed=0, failed_codes=[], run_url=None, duration_secs=1.0,
            finished_at=int(datetime.now(UTC).timestamp()) - 3600,
        )

    def test_ops_check自身は空振り判定に入らない(self) -> None:
        """実測で jss_job_runs の 2 行はどちらも ops_check の status='失敗'

        processed=0 だった。旧 SQL は status を見ないので、3 日続けば
        「処理ゼロで成功している」と自分自身を通報していた。
        """
        store = _FakeStore()
        for i in range(5):
            ops.record_job_run(
                store, job_name="ops_check", status=runner.STATUS_FAILURE, processed=0,
                failed=1, failed_codes=["slo"], run_url=None, duration_secs=1.0,
                finished_at=int(datetime.now(UTC).timestamp()) - i,
            )
        problems: list[str] = []
        ops_check._check_idle_runs(store, problems)
        assert problems == []

    def test_失敗した実行は空振りに数えない(self) -> None:
        store = _FakeStore()
        for i in range(5):
            ops.record_job_run(
                store, job_name="edinet_daily", status=runner.STATUS_FAILURE,
                processed=0, failed=3, failed_codes=["x"], run_url=None,
                duration_secs=1.0, finished_at=int(datetime.now(UTC).timestamp()) - i,
            )
        problems: list[str] = []
        ops_check._check_idle_runs(store, problems)
        assert problems == []

    def test_処理ゼロの成功が続く収集ジョブは通報する(self) -> None:
        """EDINET の 11 営業日連続 processed=0 を拾う本筋。"""
        store = _FakeStore()
        for i in range(5):
            ops.record_job_run(
                store, job_name="edinet_daily", status=runner.STATUS_SUCCESS,
                processed=0, failed=0, failed_codes=[], run_url=None,
                duration_secs=1.0, finished_at=int(datetime.now(UTC).timestamp()) - i,
            )
        problems: list[str] = []
        ops_check._check_idle_runs(store, problems)
        assert len(problems) == 1
        assert "edinet_daily" in problems[0]

    def test_30日より前の実行は空振り判定に入らない(self) -> None:
        """直近窓が無いと昔の不調をいつまでも通報し続ける (L-17)。"""
        store = _FakeStore()
        old = int((datetime.now(UTC) - timedelta(days=40)).timestamp())
        for i in range(5):
            store.con.execute(
                "INSERT INTO jss_job_runs (job_name, status, processed, failed,"
                " failed_codes, run_url, duration_secs, finished_at)"
                " VALUES ('edinet_daily', '成功', 0, 0, NULL, NULL, NULL, ?)",
                (old - i,),
            )
        store.con.commit()
        problems: list[str] = []
        ops_check._check_idle_runs(store, problems)
        assert problems == []

    def test_観測ジョブが失敗しかしていないなら止まっていると報告する(self) -> None:
        """status を絞らないと runner が status に関わらず 1 行書くので

        「毎日失敗していても生きている」を返してしまう。
        """
        store = _FakeStore()
        ops.record_job_run(
            store, job_name="freshness_probe", status=runner.STATUS_FAILURE,
            processed=0, failed=7, failed_codes=["x"], run_url=None,
            duration_secs=1.0, finished_at=int(datetime.now(UTC).timestamp()),
        )
        problems: list[str] = []
        ops_check._check_probe_alive(store, problems)
        assert len(problems) == 1
        assert "成功していない" in problems[0]

    def test_観測ジョブが黙って2日経てば報告する(self) -> None:
        store = _FakeStore()
        ops.record_job_run(
            store, job_name="freshness_probe", status=runner.STATUS_SUCCESS,
            processed=7, failed=0, failed_codes=[], run_url=None, duration_secs=1.0,
            finished_at=int((datetime.now(UTC) - timedelta(days=3)).timestamp()),
        )
        problems: list[str] = []
        ops_check._check_probe_alive(store, problems)
        assert len(problems) == 1
        assert "止まっている" in problems[0]

    def test_鮮度表が空ならfreshness_probeを促す(self, caplog) -> None:
        """空を「問題なし」と読ませない。かつ何をすればよいかを書く。"""
        import logging

        store = _FakeStore()
        ctx = TestFreshnessProbe()._ctx()
        problems: list[str] = []
        with caplog.at_level(logging.WARNING):
            ops_check._check_freshness(ctx, store, problems)
        assert ctx.failed == 1
        assert "freshness_probe" in caplog.text
        assert problems == []

    def test_宣言済みの赤だけならexit0(self, monkeypatch) -> None:
        """毎日必ず鳴る判定は通知を殺す。既知の赤は警告に落とす。

        本番の `ACCEPTED_RED` は空になった（優待は NOT_REFRESHED へ移った）が、
        仕組みは残しているので、受容を差し込んで仕組みそのものを確かめる。
        """
        store = _FakeStore()
        self._seed_freshness(store, overrides={"core_stocks": (None, 4445, 1)})
        self._seed_probe_ok(store)
        _wire(monkeypatch, store, ops_check)
        monkeypatch.setattr(slo, "ACCEPTED_RED", {"core_stocks": "テスト用の受容理由"})
        assert ops_check.main([], env=dict(_D1_ENV)) == 0

    def test_判定対象外は何日止まっていてもexit0で理由をログに出す(
        self, monkeypatch, caplog
    ) -> None:
        import logging

        store = _FakeStore()
        self._seed_freshness(store)  # yutai_benefits は 82 日前
        self._seed_probe_ok(store)
        _wire(monkeypatch, store, ops_check)
        with caplog.at_level(logging.INFO):
            assert ops_check.main([], env=dict(_D1_ENV)) == 0
        lines = [r.getMessage() for r in caplog.records if "yutai_benefits" in r.getMessage()]
        assert any("判定対象外" in m and slo.NOT_REFRESHED["yutai_benefits"] in m for m in lines)
        # 毎日必ず出る行を warning にしない（読まれなくなる）
        assert not [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "yutai_benefits" in r.getMessage()
        ]

    def test_判定対象外でも0行ならexit1(self, monkeypatch) -> None:
        """再取得不能な資産が消えたことは加齢と無関係に報告する。"""
        store = _FakeStore()
        self._seed_freshness(store, overrides={"yutai_benefits": (None, 0, None)})
        self._seed_probe_ok(store)
        _wire(monkeypatch, store, ops_check)
        assert ops_check.main([], env=dict(_D1_ENV)) == 1

    def test_判定対象外でも鮮度表に載っていなければexit1(self, monkeypatch) -> None:
        """観測は続けると宣言したので、観測が消えたら報告する。"""
        store = _FakeStore()
        self._seed_freshness(store)
        store.query("DELETE FROM jss_dataset_freshness WHERE dataset = 'yutai_benefits'")
        self._seed_probe_ok(store)
        _wire(monkeypatch, store, ops_check)
        assert ops_check.main([], env=dict(_D1_ENV)) == 1

    def test_宣言外の赤があればexit1(self, monkeypatch) -> None:
        store = _FakeStore()
        self._seed_freshness(
            store, overrides={"d1_core_stock_financials": ("2026-01-01", 3764, 1)}
        )
        self._seed_probe_ok(store)
        _wire(monkeypatch, store, ops_check)
        assert ops_check.main([], env=dict(_D1_ENV)) == 1

    def test_financialsの0行は宣言外の赤としてexit1(self, monkeypatch) -> None:
        """受容宣言を外した目的そのもの。

        writer が出来た後に `jss_financials` が 0 行なら、writer が動いていない
        か PK 移行が未適用で ON CONFLICT が失敗している。受容したままだと
        「実装済みの writer が黙って死んでいる」が沈黙する。
        """
        store = _FakeStore()
        self._seed_freshness(store, overrides={"financials": (None, 0, None)})
        self._seed_probe_ok(store)
        _wire(monkeypatch, store, ops_check)
        assert ops_check.main([], env=dict(_D1_ENV)) == 1

    def test_宣言済みが直ったら失敗させない(self, monkeypatch) -> None:
        """「宣言を外せる」は報告するが exit コードは落とさない。"""
        store = _FakeStore()
        self._seed_freshness(store)  # core_stocks は緑
        self._seed_probe_ok(store)
        _wire(monkeypatch, store, ops_check)
        monkeypatch.setattr(slo, "ACCEPTED_RED", {"core_stocks": "テスト用の受容理由"})
        assert ops_check.main([], env=dict(_D1_ENV)) == 0

    def test_鮮度表へは何も書き込まない(self, monkeypatch) -> None:
        """ops_check は判定だけ。鮮度の記録は freshness_probe の責務。

        jss_job_runs の 1 行は runner が全ジョブ共通で書くので対象外
        （⑦ の実行履歴が無いと「走ったのに黙っている」と区別できない）。
        """
        store = _FakeStore()
        self._seed_freshness(store)
        self._seed_probe_ok(store)
        _wire(monkeypatch, store, ops_check)
        before = len([s for s in store.write_sql if "jss_dataset_freshness" in s])
        ops_check.main([], env=dict(_D1_ENV))
        after = [s for s in store.write_sql if "jss_dataset_freshness" in s]
        assert len(after) == before, after[before:]
