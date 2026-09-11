"""JPX 信用残の R2 互換シム書き込み (移行 P2) のテスト。

最重要は**既存週を壊さないこと**。既存10週のうち 2026-06-12〜07-31 は JPX が
公開を終えており再取得できない（07-03・07-10 は恒久欠測）。
`margin/weeks.json` は kabulab-cf の `/api/margin` の唯一の入口で、
`JSON.parse(...).slice(-n)` で読まれるため**配列のまま**でなければならない。
"""

from __future__ import annotations

import pytest

from jp_stock_pipeline.cloud_store import margin as cm
from jp_stock_pipeline.cloud_store.guards import GuardError
from jp_stock_pipeline.collectors.jpx_margin import MarginData, MarginRow


def _data(week: str = "2026-09-11", codes: tuple[str, ...] = ("1301", "7203")) -> MarginData:
    return MarginData(
        week=week,
        rows=[
            MarginRow(code=c, sell=100, sell_chg=-10, buy=200, buy_chg=20) for c in codes
        ],
    )


class _FakeR2:
    """R2Store の最小スタブ（実通信なし）。ガードは本物を通す。"""

    def __init__(self, objects: dict | None = None, writer: str = "margin_weekly"):
        self.objects = dict(objects or {})
        self.writer = writer
        self.puts: list[str] = []

    def get_json(self, key):
        if key in self.objects:
            return self.objects[key], True
        return None, False

    def put_json_guarded(self, key, payload, *, contract=None, add_writer=True):
        from jp_stock_pipeline.cloud_store.guards import check_no_regression

        if add_writer:
            payload = dict(payload)
            payload.setdefault("writer", self.writer)
        check_no_regression(
            self.objects.get(key), payload, writer=self.writer,
            contract=contract, require_writer=add_writer,
        )
        self.objects[key] = payload
        self.puts.append(key)


class TestSnapshotKey:
    def test_uses_existing_naming(self):
        assert cm.snapshot_key("2026-09-04") == "margin/2026-09-04.json"

    def test_empty_week_is_rejected(self):
        """基準日を特定できない PDF は書き込まない（推測で日付を作らない）。"""
        with pytest.raises(ValueError, match="week が空"):
            cm.snapshot_key("")


class TestMergeWeeks:
    def test_appends_and_sorts(self):
        assert cm.merge_weeks(["2026-09-04"], "2026-08-28") == [
            "2026-08-28", "2026-09-04",
        ]

    def test_existing_week_is_not_duplicated(self):
        assert cm.merge_weeks(["2026-09-04"], "2026-09-04") == ["2026-09-04"]

    def test_first_write_starts_from_empty(self):
        assert cm.merge_weeks(None, "2026-09-04") == ["2026-09-04"]

    def test_never_drops_existing_weeks(self):
        existing = [f"2026-0{m}-0{d}" for m in (6, 7) for d in (1, 2, 3)]
        merged = cm.merge_weeks(existing, "2026-09-04")
        assert set(existing) <= set(merged)


class TestWriteMarginSnapshot:
    def test_creates_new_snapshot_without_writer_key(self):
        """行キーは公開 API に露出する。契約どおり5キーのままにする。"""
        store = _FakeR2()
        key, written = cm.write_margin_snapshot(store, _data())
        assert written is True
        assert key == "margin/2026-09-11.json"
        payload = store.objects[key]
        assert "writer" not in payload
        assert list(payload["rows"][0]) == ["code", "sell", "sell_chg", "buy", "buy_chg"]

    def test_existing_snapshot_is_not_overwritten_by_default(self):
        """既存週の不可侵。再取得できない週を上書きしない。"""
        key = "margin/2026-06-12.json"
        original = {"week": "2026-06-12", "rows": [{"code": "1301", "sell": 1,
                                                    "sell_chg": 0, "buy": 2, "buy_chg": 0}]}
        store = _FakeR2({key: original})
        _key, written = cm.write_margin_snapshot(store, _data(week="2026-06-12"))
        assert written is False
        assert store.objects[key] == original  # 1バイトも変わっていない
        assert store.puts == []

    def test_identical_re_fetch_is_a_noop(self):
        data = _data(week="2026-09-04")
        store = _FakeR2({"margin/2026-09-04.json": data.to_dict()})
        _key, written = cm.write_margin_snapshot(store, data)
        assert written is False

    def test_explicit_overwrite_is_possible_but_opt_in(self):
        key = "margin/2026-09-04.json"
        store = _FakeR2({key: {"week": "2026-09-04", "rows": []}})
        _key, written = cm.write_margin_snapshot(
            store, _data(week="2026-09-04"), overwrite_existing=True
        )
        assert written is True


class TestUpdateWeeksIndex:
    def test_appends_to_existing_index(self):
        store = _FakeR2({cm.WEEKS_KEY: ["2026-08-28", "2026-09-04"]})
        weeks = cm.update_weeks_index(store, "2026-09-11")
        assert weeks == ["2026-08-28", "2026-09-04", "2026-09-11"]
        assert store.objects[cm.WEEKS_KEY] == weeks

    def test_stays_a_plain_array_not_an_object(self):
        """/api/margin が JSON.parse(...).slice(-n) で読む。オブジェクト化は破壊的。"""
        store = _FakeR2()
        cm.update_weeks_index(store, "2026-09-11")
        assert isinstance(store.objects[cm.WEEKS_KEY], list)

    def test_guard_blocks_a_shrinking_index(self):
        """既存要素を落とす書込は拒否される（画面上データが消えるのと同じになる）。"""
        store = _FakeR2({cm.WEEKS_KEY: ["2026-06-12", "2026-06-19", "2026-07-31"]})

        # merge を通さず直接縮める書込を試みる
        with pytest.raises(GuardError):
            store.put_json_guarded(cm.WEEKS_KEY, ["2026-09-11"], add_writer=False)

    def test_unexpected_shape_is_rejected_not_overwritten(self):
        """weeks.json がオブジェクトだった場合、上書きせず失敗させる。"""
        store = _FakeR2({cm.WEEKS_KEY: {"weeks": ["2026-09-04"]}})
        with pytest.raises(GuardError, match="配列ではない"):
            cm.update_weeks_index(store, "2026-09-11")

    def test_existing_weeks_survive_a_normal_update(self):
        """再取得不能な既存10週が通常更新で失われないこと。"""
        existing = [
            "2026-06-12", "2026-06-19", "2026-06-26", "2026-07-17", "2026-07-24",
            "2026-07-31", "2026-08-07", "2026-08-14", "2026-08-21", "2026-08-28",
        ]
        store = _FakeR2({cm.WEEKS_KEY: list(existing)})
        weeks = cm.update_weeks_index(store, "2026-09-04")
        assert set(existing) <= set(weeks)
        assert len(weeks) == 11
