"""突合検証 (§3-5) のテスト: 閾値判定・要確認フラグ・値を書き換えないこと。

第2ソースは抽象化されている（J-Quants 廃止後は stooq が既定）。突合機構自体は
ソース非依存なので、ここではソースに依らず Code/Close の対で検証する。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from jp_stock_pipeline.licensing import LicenseTag
from jp_stock_pipeline.models import (
    DataQuality,
    PriceTechnicalRecord,
    Provenance,
    Source,
    now_jst,
)
from jp_stock_pipeline.transform.reconcile import (
    ReconcileDataError,
    adopt_confirmed_value,
    apply_flags,
    normalize_code,
    reconcile_prices,
)


def record(code: str, close: float | None) -> PriceTechnicalRecord:
    return PriceTechnicalRecord(
        code=code,
        close=close,
        provenance=Provenance(
            source=Source.YFINANCE,
            license_tag=LicenseTag.PERSONAL_ONLY,
            data_date=date(2026, 6, 10),
            fetched_at=now_jst(),
        ),
    )


def other_frame(rows: list[tuple[str, float | None]]) -> pd.DataFrame:
    """第2ソースの (Code, Close) フレーム。コードは 5 桁/4 桁いずれも許容。"""
    return pd.DataFrame([{"Code": c, "Close": v} for c, v in rows])


class TestNormalizeCode:
    def test_five_digit_to_four(self):
        assert normalize_code("72030") == "7203"

    def test_four_digit_unchanged(self):
        assert normalize_code("7203") == "7203"

    def test_種類株コードを普通株のキーにしない(self):
        """旧実装は "25935"（伊藤園 第1種優先株式）を "2593"（同社 普通株）の
        キーにしていた。第2ソースに種類株の終値が混ざると、普通株の終値と
        比べて偽の乖離を報告する（あるいは本物の乖離を塗り潰す）。
        """
        assert normalize_code("25935") is None

    def test_Noneが文字列Noneにならない(self):
        """旧実装は `str(raw)` を通していたため、None が突合辞書に `"None"`
        というキーを作っていた。
        """
        assert normalize_code(None) is None

    def test_実在しない形式を素通しさせない(self):
        assert normalize_code("07203") is None  # "0720" を捏造していた
        assert normalize_code("7203.T") is None
        assert normalize_code("A130") is None


class TestOurSideCorruption:
    """② 側の非正準コードは黙って飛ばさず例外にする。

    `record.code` は ① 銘柄マスタ由来で既に正準4文字である前提。ここで
    正規化に失敗するのは「② 自体がデータ破損している」ということで、
    それは突合検証がまさに見つけるべきもの。第2ソース側と同じように
    スキップすると検証の目的を取り落とす（§3-1 の「欠損は欠損」は
    取得できなかった値の話で、破損を飛ばす許可ではない）。
    """

    def test_非正準コードはReconcileDataError(self):
        records = [record("25935", 3000.0)]
        other = other_frame([("25930", 3100.0)])
        with pytest.raises(ReconcileDataError, match="25935"):
            reconcile_prices(other, records, threshold_pct=1.0)

    def test_正準コードなら止まらない(self):
        records = [record("2593", 3000.0)]
        other = other_frame([("25930", 3100.0)])
        assert len(reconcile_prices(other, records, threshold_pct=1.0)) == 1


class TestReconcile:
    def test_deviation_over_threshold_detected(self):
        records = [record("7203", 3000.0)]
        other = other_frame([("72030", 3100.0)])  # 約3.2%乖離
        result = reconcile_prices(other, records, threshold_pct=1.0)
        assert len(result) == 1
        d = result[0]
        assert d.code == "7203"
        assert d.ours == 3000.0
        assert d.theirs == 3100.0
        assert d.deviation_pct > 3.0

    def test_within_threshold_not_flagged(self):
        records = [record("7203", 3099.0)]
        other = other_frame([("72030", 3100.0)])  # 0.03%
        assert reconcile_prices(other, records, threshold_pct=1.0) == []

    def test_missing_value_not_compared(self):
        """欠損は欠損のまま (§3-1)。乖離扱いもしない。"""
        records = [record("7203", None), record("9999", 100.0)]
        other = other_frame([("72030", 3100.0), ("99990", None)])
        assert reconcile_prices(other, records, threshold_pct=1.0) == []

    def test_threshold_configurable(self):
        records = [record("7203", 3050.0)]
        other = other_frame([("72030", 3100.0)])  # 約1.6%
        assert len(reconcile_prices(other, records, threshold_pct=1.0)) == 1
        assert reconcile_prices(other, records, threshold_pct=2.0) == []

    def test_four_digit_source_code_matches(self):
        """stooq 等の 4 桁コードでも突合できる。"""
        records = [record("7203", 3000.0)]
        other = other_frame([("7203", 3100.0)])
        assert len(reconcile_prices(other, records, threshold_pct=1.0)) == 1

    def test_empty_other(self):
        assert reconcile_prices(pd.DataFrame(), [record("7203", 100.0)]) == []


class TestApplyFlags:
    def test_flag_set_and_values_untouched(self):
        records = [record("7203", 3000.0), record("6758", 2000.0)]
        other = other_frame([("72030", 3100.0), ("67580", 2001.0)])
        discrepancies = reconcile_prices(other, records, threshold_pct=1.0)
        flagged = apply_flags(records, discrepancies)

        assert [r.code for r in flagged] == ["7203"]
        assert records[0].provenance.quality is DataQuality.NEEDS_REVIEW
        assert records[0].close == 3000.0  # 値は書き換えない (§3-5)
        assert records[1].provenance.quality is DataQuality.OK
        assert records[1].close == 2000.0


class TestAdoptConfirmedValue:
    def test_explicit_adoption_updates_source(self):
        """明示的採用時のみ値が変わり、採用元ソース名が更新される (§3-5)。"""
        rec = record("7203", 3000.0)
        rec.provenance.quality = DataQuality.NEEDS_REVIEW
        adopt_confirmed_value(rec, 3100.0, Source.STOOQ)
        assert rec.close == 3100.0
        assert rec.provenance.source is Source.STOOQ
        assert rec.provenance.quality is DataQuality.OK
