"""ライセンスタグ継承ルール (§2.2) のテスト。"""

import pytest

from jp_stock_pipeline.licensing import (
    LicenseTag,
    inherit,
    is_metadata_publishable,
    is_publishable,
    source_license,
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
