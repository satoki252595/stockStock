"""⑨優待の派生値退避 (移行 P3) のテスト。

守るべき不変条件は2つ:
1. 出典サイトの掲載文 `description` を**退避対象に混ぜない**
2. 元データが壊れた状態を「正しい退避」として索引に載せない
"""

from __future__ import annotations

import pytest

from jp_stock_pipeline.cloud_store import yutai
from jp_stock_pipeline.cloud_store.guards import GuardError, check_no_regression


def _row(rid: int, *, value: int | None = 3000, summary: str = "3,000円相当") -> dict:
    return {
        "id": rid,
        "code": "7203",
        "genre": "食品・飲料",
        "min_shares": 100,
        "record_month": 3,
        "short_summary": summary,
        "estimated_value": value,
    }


class TestExportColumns:
    def test_description_を選択列に含めない(self) -> None:
        assert "description" not in yutai.EXPORT_COLUMNS
        # SQL 側にも出てこないこと。`short_summary` は別語なので単語境界で見る。
        assert " b.description" not in yutai.EXPORT_SQL
        assert "description AS" not in yutai.EXPORT_SQL

    def test_契約キーは選択列と一致する(self) -> None:
        assert yutai.SNAPSHOT_CONTRACT["rows[]"] == yutai.EXPORT_COLUMNS

    def test_退避対象外の列が混ざったら組み立てを拒む(self) -> None:
        bad = _row(1) | {"description": "みんかぶの掲載文"}
        with pytest.raises(GuardError, match="退避対象外の列"):
            yutai.build_snapshot([bad], source_db="db", counts=yutai.count_rows([bad]))


class TestFetchRows:
    def test_id_のキーセットで全件辿る(self) -> None:
        all_rows = [_row(i) for i in range(1, 6)]
        calls: list[list] = []

        def query(sql: str, params: list) -> list[dict]:
            calls.append(params)
            last_id, limit = params
            return [r for r in all_rows if r["id"] > last_id][:limit]

        got = yutai.fetch_rows(query, page_size=2)
        assert [r["id"] for r in got] == [1, 2, 3, 4, 5]
        # OFFSET ではなく直前の id を渡して進んでいること
        assert [p[0] for p in calls] == [0, 2, 4]

    def test_空テーブルでも落ちない(self) -> None:
        assert yutai.fetch_rows(lambda sql, params: [], page_size=2) == []


class TestCounts:
    def test_推定額と要約の件数を数える(self) -> None:
        rows = [_row(1), _row(2, value=None), _row(3, summary="  ")]
        assert yutai.count_rows(rows) == {"rows": 3, "with_value": 2, "with_summary": 2}


class TestCheckFloors:
    def _counts(self, **over: int) -> dict[str, int]:
        base = {
            "rows": yutai.BASELINE_ROWS,
            "with_value": yutai.BASELINE_WITH_VALUE,
            "with_summary": yutai.BASELINE_WITH_SUMMARY,
        }
        return base | over

    def test_基準どおりなら通る(self) -> None:
        yutai.check_floors(self._counts())

    def test_推定額が基準を大きく下回ったら止める(self) -> None:
        with pytest.raises(GuardError, match="with_value"):
            yutai.check_floors(self._counts(with_value=100))

    def test_前回より減ったら止める(self) -> None:
        previous = self._counts()
        with pytest.raises(GuardError, match="前回退避より減った"):
            yutai.check_floors(self._counts(rows=yutai.BASELINE_ROWS - 1), previous=previous)

    def test_allow_shrink_なら前回より減っても通す(self) -> None:
        previous = self._counts()
        yutai.check_floors(
            self._counts(rows=yutai.BASELINE_ROWS - 1), previous=previous, allow_shrink=True
        )

    def test_allow_shrink_でも絶対下限は超えられない(self) -> None:
        with pytest.raises(GuardError, match="rows"):
            yutai.check_floors(self._counts(rows=10), allow_shrink=True)


class TestSnapshot:
    def test_同じ中身なら同じバイト列になる(self) -> None:
        counts = yutai.count_rows([_row(1), _row(2)])
        a = yutai.build_snapshot([_row(2), _row(1)], source_db="db", counts=counts)
        b = yutai.build_snapshot([_row(1), _row(2)], source_db="db", counts=counts)
        # 取得時刻を持たないので、行順が違っても同一内容なら同一バイト列になる
        assert yutai.serialize(a) == yutai.serialize(b)

    def test_契約キーを満たす(self) -> None:
        rows = [_row(1)]
        snap = yutai.build_snapshot(rows, source_db="db", counts=yutai.count_rows(rows))
        check_no_regression(None, snap, writer="yutai_backup",
                            contract=yutai.SNAPSHOT_CONTRACT, require_writer=False)

    def test_キーは内容ハッシュだけで決まる(self) -> None:
        key = yutai.snapshot_key("a" * 64)
        assert key == f"backup/yutai/{'a' * yutai.SHA_PREFIX_LEN}.json"
        with pytest.raises(ValueError):
            yutai.snapshot_key("")


class TestIndex:
    def test_追記しても既存要素を落とさない(self) -> None:
        first = {"taken_at": "t1", "key": "k1", "sha256": "s1", "counts": {"rows": 1}}
        second = {"taken_at": "t2", "key": "k2", "sha256": "s2", "counts": {"rows": 2}}
        index = yutai.append_index(None, first)
        assert yutai.latest_entry(index) == first
        grown = yutai.append_index(index, second)
        assert yutai.latest_entry(grown) == second
        # 後退禁止ガードを通ること（既存要素が消えていない）
        check_no_regression(index, grown, writer="yutai_backup",
                            contract=yutai.INDEX_CONTRACT, require_writer=False)

    def test_壊れた索引は最新なしとして扱う(self) -> None:
        assert yutai.latest_entry(None) is None
        assert yutai.latest_entry({"snapshots": []}) is None
        assert yutai.latest_entry({"snapshots": "x"}) is None
        assert yutai.latest_entry([1, 2]) is None
