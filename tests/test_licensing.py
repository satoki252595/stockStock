"""ライセンスタグ継承ルール (§2.2) のテスト。"""

import sqlite3

import pytest

from jp_stock_pipeline.licensing import (
    LicenseTag,
    inherit,
    is_metadata_publishable,
    is_publishable,
    source_license,
    stricter_tag_sql,
    strictness_rank,
    strictness_rank_sql,
)
from jp_stock_pipeline.models import Source


class TestInherit:
    def test_strictest_wins_personal(self):
        assert (
            inherit([LicenseTag.COMMERCIAL_OK, LicenseTag.PERSONAL_ONLY])
            is LicenseTag.PERSONAL_ONLY
        )

    def test_strictest_wins_factual(self):
        assert (
            inherit([LicenseTag.COMMERCIAL_OK, LicenseTag.FACTUAL_CITE])
            is LicenseTag.FACTUAL_CITE
        )

    def test_all_commercial_stays_commercial(self):
        assert (
            inherit([LicenseTag.COMMERCIAL_OK, LicenseTag.COMMERCIAL_OK])
            is LicenseTag.COMMERCIAL_OK
        )

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            inherit([])


class TestSourceLicense:
    def test_edinet_is_commercial_ok(self):
        assert source_license(Source.EDINET) is LicenseTag.COMMERCIAL_OK

    def test_tdnet_is_factual_cite(self):
        assert source_license(Source.TDNET) is LicenseTag.FACTUAL_CITE

    @pytest.mark.parametrize(
        "source", [Source.YFINANCE, Source.STOOQ, Source.JPX]
    )
    def test_personal_only_sources(self, source):
        assert source_license(source) is LicenseTag.PERSONAL_ONLY

    def test_calc_must_inherit(self):
        with pytest.raises(ValueError):
            source_license(Source.CALC)


class TestPublishFilter:
    def test_only_commercial_fully_publishable(self):
        assert is_publishable(LicenseTag.COMMERCIAL_OK)
        assert not is_publishable(LicenseTag.FACTUAL_CITE)
        assert not is_publishable(LicenseTag.PERSONAL_ONLY)

    def test_metadata_publishable(self):
        assert is_metadata_publishable(LicenseTag.COMMERCIAL_OK)
        assert is_metadata_publishable(LicenseTag.FACTUAL_CITE)
        assert not is_metadata_publishable(LicenseTag.PERSONAL_ONLY)


class TestStrictnessRankSql:
    """SQL で再現した順位が Python の順位と一致すること。

    ③ は D1(SQLite) とローカル PG の両方がこの式で「厳しい側を残す」。
    式は両方言に共通の CASE だけなので sqlite で評価して確かめる。
    """

    @staticmethod
    def _eval(expression: str, *params: str):
        return sqlite3.connect(":memory:").execute(f"SELECT {expression}", params).fetchone()[0]

    @pytest.mark.parametrize("tag", list(LicenseTag))
    def test_matches_the_python_rank(self, tag):
        assert self._eval(strictness_rank_sql("?"), tag.value) == strictness_rank(tag)

    def test_unknown_tag_ranks_as_the_strictest(self):
        """未知のタグを緩い側へ倒すと公開フィルタを素通りする。"""
        strictest = max(strictness_rank(t) for t in LicenseTag)
        assert self._eval(strictness_rank_sql("?"), "mystery") == strictest + 1

    @pytest.mark.parametrize(
        ("incoming", "existing", "expected"),
        [
            ("commercial-ok", "factual-cite", "factual-cite"),
            ("factual-cite", "commercial-ok", "factual-cite"),
            ("personal-only", "factual-cite", "personal-only"),
            ("commercial-ok", "commercial-ok", "commercial-ok"),
            ("commercial-ok", "mystery", "mystery"),
        ],
    )
    def test_stricter_tag_sql_keeps_the_stricter_side(self, incoming, existing, expected):
        sql = stricter_tag_sql("?1", "?2")
        assert self._eval(sql, incoming, existing) == expected
