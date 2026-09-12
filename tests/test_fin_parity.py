"""G-fin-1（③断面の writer 交代判定）の再定義テスト。

旧定義「数値列の相対誤差 ≤ 1e-6」は原理的に通らない。両 writer は別時刻に
Yahoo を叩き、Yahoo は TTM を改訂するので eps/bps/roe は値そのものが変わり、
price は日中に動く。実測（2026-09-12）:

  per   3,317 / 3,317 一致 (worst 1.313e-07)
  pbr   3,736 / 3,736 一致 (worst 1.368e-07)
  eps   1,611 中 最厳 76 / 最緩 1,465
  bps   1,612 中 648 → 949
  roe   1,517 中 744 → 806
  price 1,616 中 最厳 23 / 最緩 314

本テストが固定するのは次の 3 点。

1. **項目ごとに閾値が違う**こと（per/pbr だけ 1e-6、他は閾値なし）
2. **「閾値なし」を「一致した」と読ませない**こと（レポートに観測のみと出る）
3. 値一致に代えて置いた **導出整合 / 符号 / 欠損パターン**が実際に壊れ方を捕まえる
"""

from __future__ import annotations

import pytest

from jp_stock_pipeline.cloud_store.fin_parity import (
    DERIVATION_REL_TOL,
    DERIVED_IDENTITIES,
    FIELD_RULES,
    MAX_NEW_ONLY_NULL_RATIO,
    PUBLISHED_REL_TOL,
    check_row_consistency,
    compare_field,
    evaluate_parity,
    relative_difference,
)

# 実測（2026-09-12）。閾値の根拠なので値としてテストへ持つ。
WORST_PER = 1.313e-07
WORST_PBR = 1.368e-07


class TestOldDefinitionIsUnreachable:
    """「1e-6 を全項目に課す」が原理的に通らないことを実測値で固定する。"""

    def test_per_と_pbr_だけが_1e_6_を満たす(self) -> None:
        assert WORST_PER <= PUBLISHED_REL_TOL
        assert WORST_PBR <= PUBLISHED_REL_TOL
        assert FIELD_RULES["per"].rel_tol == PUBLISHED_REL_TOL
        assert FIELD_RULES["pbr"].rel_tol == PUBLISHED_REL_TOL

    @pytest.mark.parametrize("name", ["eps", "bps", "roe", "price"])
    def test_TTM_改訂を受ける項目には値一致を課さない(self, name: str) -> None:
        """ここを 1e-6 に戻すと、eps は 1,611 行中 1,535 行で落ちる。"""
        assert FIELD_RULES[name].rel_tol is None, (
            f"{name} に値一致の閾値を戻した。実測で一致しないので毎回落ちる:"
            f" {FIELD_RULES[name].evidence}"
        )

    def test_全項目に閾値の根拠が付いている(self) -> None:
        """根拠の無い数字を足させない。1e-6 が生き残ったのは根拠が無かったから。"""
        for name, rule in FIELD_RULES.items():
            assert rule.evidence.strip(), name
            assert "実測" in rule.evidence, name

    def test_閾値なしの項目は観測のみと明示される(self) -> None:
        """「落ちなかった」を「一致した」と読み替えさせないための表示。"""
        verdict = compare_field("eps", [(100.0, 103.0), (50.0, 50.0)])
        assert verdict.observed_only
        assert verdict.ok, "閾値なしの項目は相対差だけでは落とさない"
        assert "値一致は判定していない" in verdict.summary()
        assert verdict.matched is None


class TestRelativeDifference:
    def test_分母は大きい側で取る(self) -> None:
        """old を分母にすると old≈0 の行で発散し、閾値の意味が行ごとに変わる。"""
        assert relative_difference(1e-12, 1.0) == pytest.approx(1.0)
        assert relative_difference(1.0, 1e-12) == pytest.approx(1.0)

    def test_両方ゼロは一致(self) -> None:
        assert relative_difference(0.0, 0.0) == 0.0

    def test_片方だけ_NULL_は比較不能(self) -> None:
        assert relative_difference(None, 1.0) is None
        assert relative_difference(1.0, None) is None

    def test_符号が逆なら相対差は_1_以上(self) -> None:
        assert relative_difference(10.0, -10.0) == pytest.approx(2.0)


class TestPerFieldComparableCount:
    """旧定義の欠陥: 比較可能性を行単位でしか切っていなかった。"""

    def test_項目ごとに比較可能行数を数える(self) -> None:
        """実測では母数が 1,517〜3,736 と 2 倍以上違う。行単位では見えない。"""
        old = {
            "a": {"per": 10.0, "eps": 5.0},
            "b": {"per": 11.0, "eps": None},
            "c": {"per": 12.0, "eps": None},
        }
        new = {
            "a": {"per": 10.0, "eps": 5.0},
            "b": {"per": 11.0, "eps": 6.0},
            "c": {"per": 12.0, "eps": None},
        }
        report = evaluate_parity(old, new)
        by_field = {v.field: v for v in report.verdicts}
        assert by_field["per"].both_present == 3
        assert by_field["eps"].both_present == 1
        assert by_field["eps"].old_only_null == 1
        assert by_field["eps"].both_null == 1

    def test_基準日が違う行は比較から外して数える(self) -> None:
        old = {"a": {"per": 10.0}, "b": {"per": 11.0}}
        new = {"a": {"per": 10.0}, "b": {"per": 99.0}}
        report = evaluate_parity(
            old, new, base_dates={"a": ("2026-09-11", "2026-09-11"), "b": ("2026-09-10", "2026-09-11")}
        )
        assert report.incomparable_rows == 1
        assert report.ok, "基準日の違う行を混ぜて per の一致判定を落としている"

    def test_片側にしか無い銘柄も比較不能に数える(self) -> None:
        report = evaluate_parity({"a": {"per": 1.0}, "b": {"per": 1.0}}, {"a": {"per": 1.0}})
        assert report.incomparable_rows == 1


class TestPublishedValueTolerance:
    """G-fin-1a: per / pbr は 1e-6 を据え置く。"""

    def test_実測_worst_の乖離なら通る(self) -> None:
        verdict = compare_field("per", [(10.0, 10.0 * (1 + WORST_PER))])
        assert verdict.ok, verdict.problems
        assert verdict.matched == 1

    def test_1e_6_を超えたら落とす(self) -> None:
        verdict = compare_field("pbr", [(1.0, 1.0 + 1e-5)])
        assert not verdict.ok
        assert any("G-fin-1a" in p for p in verdict.problems), verdict.problems

    def test_落としたときに根拠を出す(self) -> None:
        """なぜこの項目だけ厳しいのかが読めないと、閾値が勘で緩められる。"""
        verdict = compare_field("per", [(1.0, 2.0)])
        assert any("1.313e-07" in p for p in verdict.problems), verdict.problems


class TestSignInvariant:
    """G-fin-1c: TTM 改訂は大きさを変えるが符号は変えない。"""

    def test_eps_の符号反転を落とす(self) -> None:
        verdict = compare_field("eps", [(120.0, -118.0)])
        assert not verdict.ok
        assert any("符号が反転" in p for p in verdict.problems), verdict.problems

    def test_大きさが倍でも符号が同じなら落とさない(self) -> None:
        """TTM 改訂で起こりうる変化。ここを落とすと旧定義と同じ轍を踏む。"""
        assert compare_field("eps", [(120.0, 240.0)]).ok

    def test_price_には符号一致を課さない(self) -> None:
        """負の株価は存在しないので符号一致は常に真で、判定として空虚。"""
        assert FIELD_RULES["price"].require_sign_match is False


class TestRowConsistency:
    """G-fin-1b / 1c: 1 スナップショット内の自己整合。writer 間の一致を見ない。"""

    def test_整合した行は何も言わない(self) -> None:
        row = {"price": 3000.0, "eps": 200.0, "per": 15.0, "bps": 2500.0, "pbr": 1.2}
        assert check_row_consistency(row) == []

    def test_100倍の単位ミスを捕まえる(self) -> None:
        """導出整合の 1% は精度ではなく桁の検査。×100 は必ず捕まる。"""
        row = {"price": 3000.0, "eps": 200.0, "per": 1500.0}
        problems = check_row_consistency(row)
        assert any("G-fin-1b" in p for p in problems), problems

    def test_丸め程度のずれは通す(self) -> None:
        """提供元は per を小数2桁で配信する。丸めで落ちては使えない。"""
        row = {"price": 3000.0, "eps": 200.0, "per": 15.0 * (1 + DERIVATION_REL_TOL / 2)}
        assert check_row_consistency(row) == []

    def test_赤字なのに_PER_が正なら落とす(self) -> None:
        """符号を落として絶対値だけ入れる実装ミスを捕まえる。"""
        problems = check_row_consistency({"eps": -50.0, "per": 30.0})
        assert any("eps=-50.0 が負なのに" in p for p in problems), problems

    def test_債務超過なのに_PBR_が正なら落とす(self) -> None:
        problems = check_row_consistency({"bps": -100.0, "pbr": 0.8})
        assert any("G-fin-1c" in p for p in problems), problems

    def test_株価が正でなければ落とす(self) -> None:
        assert check_row_consistency({"price": 0.0})

    def test_分母がゼロや_NULL_の行は検証不能として飛ばす(self) -> None:
        """捏造で埋めない（§3-1）。0 除算もしない。"""
        assert check_row_consistency({"price": 100.0, "eps": 0.0, "per": 12.0}) == []
        assert check_row_consistency({"price": 100.0, "per": 12.0}) == []

    def test_roe_は導出恒等式に入れない(self) -> None:
        """roe の単位（% か比か）が未確認。100 倍の係数を推測で書かない。"""
        assert [name for name, _, _ in DERIVED_IDENTITIES] == ["per", "pbr"]
        row = {"eps": 200.0, "bps": 2500.0, "roe": 8.0}  # % なら整合、比なら 100 倍ずれ
        assert check_row_consistency(row) == []


class TestNullPattern:
    """G-fin-1d: 新 writer だけ NULL の項目単位の欠損。"""

    def test_新_writer_だけ_NULL_が上限を超えたら落とす(self) -> None:
        pairs = [(10.0, 10.0)] * 100 + [(10.0, None)]
        verdict = compare_field("per", pairs)
        assert verdict.new_only_null == 1
        assert not verdict.ok
        assert any("G-fin-1d" in p for p in verdict.problems), verdict.problems

    def test_上限内なら落とさない(self) -> None:
        pairs = [(10.0, 10.0)] * 1_000 + [(10.0, None)]  # 0.1% < 0.5%
        verdict = compare_field("per", pairs)
        assert verdict.ok, verdict.problems

    def test_旧だけ_NULL_は落とさない(self) -> None:
        """新 writer が値を取れるようになったのは改善で、退行ではない。"""
        verdict = compare_field("per", [(None, 10.0)] * 100)
        assert verdict.old_only_null == 100
        assert verdict.ok, verdict.problems

    def test_上限は_G_fin_2_と同じ値(self) -> None:
        assert MAX_NEW_ONLY_NULL_RATIO == 0.005


class TestReport:
    def test_レポートに観測のみの項目を列挙する(self) -> None:
        report = evaluate_parity({"a": {"per": 1.0}}, {"a": {"per": 1.0}})
        text = "\n".join(report.lines())
        assert "値一致を判定していない項目" in text
        for name in ("eps", "bps", "roe", "price"):
            assert name in text
        assert report.observed_only_fields == ("eps", "bps", "roe", "price")

    def test_行の自己整合の違反は銘柄コード付きで出す(self) -> None:
        report = evaluate_parity(
            {"7203": {"eps": -50.0, "per": 30.0}},
            {"7203": {"eps": -50.0, "per": 30.0}},
        )
        assert not report.ok
        assert any(p.startswith("7203 ") for p in report.row_problems), report.row_problems

    def test_分布を観測して載せる(self) -> None:
        """閾値を入れるのは3回連続の分布が出てから。まず観測を残す。"""
        pairs = [(100.0, 100.0 * (1 + i / 100)) for i in range(101)]
        verdict = compare_field("eps", pairs)
        assert verdict.median_rel_diff is not None
        assert verdict.p95_rel_diff is not None
        assert verdict.worst_rel_diff == pytest.approx(1.0 / 2.0)  # 100 -> 200
        assert verdict.median_rel_diff < verdict.p95_rel_diff < verdict.worst_rel_diff
