"""ジョブから Cloudflare 正本へ書くための薄い口 (docs/CF-CANONICAL-DESIGN.md)。

**書込順序は R2 → D1 で固定する。** 逆にすると「D1 に行があるのに R2 に
オブジェクトが無い」＝索引が嘘をつく状態を作る。R2 先行なら最悪でも
「未索引オブジェクト」で、無害かつ後から突合で拾える。
"""

from __future__ import annotations

import logging
import mimetypes
from datetime import date
from typing import TYPE_CHECKING

from ..config import CloudStoreSettings
from . import financials, keys
from .d1 import D1Error, D1Store
from .r2 import R2Error, R2Store

if TYPE_CHECKING:
    from ..models import FinancialSummaryRecord, RawArtifact

logger = logging.getLogger(__name__)

# jss_raw_files の列。R2 キーと1対1で対応する索引なので、本体データは持たない。
_RAW_COLUMNS = (
    "sha256", "r2_bucket", "r2_key", "derived_key", "derived_ext",
    "source", "datatype", "scope", "doc_id", "code", "data_date", "ext",
    "size_bytes", "license_tag", "convert_status",
    "first_fetched_at", "last_fetched_at",
)

# 収集側 (edinet.py / tdnet_hourly.py) が doc_id を渡すようになる前
# (2026-09-11〜) に保存された原本は、キーの doc_id セグメントが
# `keys.DOC_ID_FALLBACK`（"_"）の「旧キー」のまま R2 にある。この日付までの
# data_date だけ旧キーを HEAD して救済し（実在すれば新キーへ PUT せず旧キー
# を索引する）、R2 のオブジェクト数を増やさない (docs/CF-CANONICAL-DESIGN.md
# §3.2)。
#
# レビュー・マージが延びても救済対象から漏れないよう、想定マージ日
# (2026-09-14 ごろ) よりかなり先の日付に余裕を持たせて設定してある。この
# 日付までは新規に保存される文書についても「新キー未実在→旧キーを HEAD」
# を1回余分に行うが、旧キーは実在しないため即座に新キーへ PUT するだけで
# R2 のオブジェクト数・バイトは増えない。増えるのは R2 Class B (HEAD) 呼出
# 回数のみで、無料枠 (1,000万回/月) に対して無視できる量 (PR #56 の
# 「コスト影響」参照)。この定数を過ぎた data_date では旧キーを HEAD しない
# ため、以後は追加コストそのものが発生しない。マージが本定数を過ぎてから
# 行われる場合は、値をマージ日以降に伸ばしてから push すること。
LEGACY_KEY_UNTIL = date(2026, 12, 31)


def _content_type(path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


class CloudSink:
    """R2 と D1 への書き込み。設定が無ければ全メソッドが None を返して何もしない。

    戻り値の意味は local_store.sink と揃える:
    True=書けた / False=書こうとして失敗 / None=無効（対象外）。
    """

    def __init__(
        self, settings: CloudStoreSettings, *, writer: str, dry_run: bool = False
    ) -> None:
        self.settings = settings
        self.writer = writer
        self.dry_run = dry_run
        self._raw_bucket: R2Store | None = None
        self._d1: D1Store | None = None
        # 1 ジョブ実行内の core_stocks.code -> id の解決結果。同じ銘柄が同日に
        # 複数の書類を出すのは普通なので、同じ SELECT を繰り返さない。
        self._stock_ids: dict[str, int | None] = {}

    @property
    def enabled(self) -> bool:
        return self.settings.enabled() and not self.dry_run

    @property
    def raw_bucket(self) -> R2Store:
        if self._raw_bucket is None:
            self._raw_bucket = R2Store(
                self.settings, self.settings.bucket_raw, writer=self.writer
            )
        return self._raw_bucket

    @property
    def d1(self) -> D1Store:
        if self._d1 is None:
            self._d1 = D1Store(self.settings, writer=self.writer)
        return self._d1

    def upsert_raw_artifact(self, artifact: "RawArtifact") -> bool | None:
        """⑤原本を R2 へ置き、D1 jss_raw_files に索引を作る。

        原本キーは SHA256 を含む immutable なので、同一内容の再取得は PUT を
        省略する（R2 にバージョニングが無いため上書き自体が起こらない設計）。

        `LEGACY_KEY_UNTIL` までの `data_date` は、doc_id 付きの新キーが無くても
        すぐには PUT せず、doc_id 無しの旧キー（收集側が doc_id を渡していな
        かった間に書かれたもの）を先に HEAD する。旧キーに実在すれば PUT を
        省略し、索引の `r2_key` にその旧キーを入れる（R2 を増やさずに doc_id
        だけ埋める。docs/CF-CANONICAL-DESIGN.md §3.2）。派生ファイルのキーは
        `derived_key(key, ...)` で作るので、`key` が旧キーに切り替われば派生も
        自動的に旧キー側の位置を指す。
        """
        if not self.enabled:
            return None
        if not self.settings.r2_enabled():
            return None
        try:
            data_date = artifact.data_date or artifact.fetched_at.date()
            ext = artifact.local_path.suffix.lstrip(".") or "bin"
            key = keys.raw_key(
                source=str(artifact.source),
                datatype=artifact.datatype,
                scope=artifact.scope,
                data_date=data_date,
                sha256=artifact.sha256,
                ext=ext,
                doc_id=artifact.doc_id,
            )
            if not self.raw_bucket.exists(key):
                legacy_key = None
                if artifact.doc_id and data_date <= LEGACY_KEY_UNTIL:
                    legacy_key = keys.raw_key(
                        source=str(artifact.source),
                        datatype=artifact.datatype,
                        scope=artifact.scope,
                        data_date=data_date,
                        sha256=artifact.sha256,
                        ext=ext,
                        doc_id=None,
                    )
                if legacy_key is not None and self.raw_bucket.exists(legacy_key):
                    # (b) 旧キー救済: 実在するので PUT せず、以後は旧キーを正とする。
                    key = legacy_key
                else:
                    self.raw_bucket.put_bytes(
                        key,
                        artifact.local_path.read_bytes(),
                        content_type=_content_type(artifact.local_path),
                    )
            derived_key = derived_ext = None
            for converted in artifact.converted_paths:
                suffix = converted.suffix.lstrip(".")
                dkey = keys.derived_key(key, suffix=suffix)
                if not self.raw_bucket.exists(dkey):
                    self.raw_bucket.put_bytes(
                        dkey, converted.read_bytes(), content_type=_content_type(converted)
                    )
                # 索引に載せるのは代表1件（複数変換版がある場合は最初のもの）。
                if derived_key is None:
                    derived_key, derived_ext = dkey, suffix
        except (R2Error, OSError) as exc:
            logger.warning("R2 ⑤原本の保存に失敗: %s: %s", artifact.filename, exc)
            return False

        if not self.settings.d1_enabled():
            # R2 には残った。索引だけ後から作れるので False にはしない。
            logger.info("D1 未設定のため jss_raw_files の索引は作らない: %s", key)
            return True
        epoch = int(artifact.fetched_at.timestamp())
        try:
            self.d1.upsert(
                "jss_raw_files",
                list(_RAW_COLUMNS),
                [[
                    artifact.sha256, self.settings.bucket_raw, key, derived_key, derived_ext,
                    str(artifact.source), artifact.datatype, artifact.scope,
                    artifact.doc_id,
                    artifact.scope if artifact.scope != "ALL" else None,
                    data_date.isoformat(), ext, artifact.size_bytes,
                    artifact.license_tag.value, artifact.convert_status.value,
                    epoch, epoch,
                ]],
                conflict=["sha256"],
                # 再取得のたびに excluded.* で上書きすると初回取得時刻が消える。
                # 既存行があれば first_fetched_at はそのまま残す。
                keep=["first_fetched_at"],
            )
        except D1Error as exc:
            # R2 には原本が残っている＝トレーサビリティは失われていない。
            logger.warning("D1 jss_raw_files への索引作成に失敗: %s: %s", key, exc)
            return False
        return True


    def prefetch_stock_ids(self, codes: list[str]) -> int | None:
        """③ の書込で使う `core_stocks.id` をまとめて先に解決しておく。

        任意の最適化。失敗しても per-code 解決へ落ちるだけなので、ここは
        例外を外へ出さない（呼び出し側にエラー処理を書かせない）。
        """
        if not self.enabled or not self.settings.d1_enabled():
            return None
        try:
            return financials.prefetch_stock_ids(
                self.d1, codes, cache=self._stock_ids
            )
        except D1Error as exc:
            logger.warning("core_stocks.id の一括解決に失敗（個別解決へ）: %s", exc)
            return None

    def upsert_financial_summary(
        self,
        record: "FinancialSummaryRecord",
        *,
        doc_id: str | None,
        raw_sha256: str | None,
    ) -> bool | None:
        """③財務サマリを D1 `jss_financials` へ upsert する。

        `raw_sha256` は ⑤原本索引 `jss_raw_files.sha256`（PK）への結合キー。
        `jss_raw_files.doc_id` は実測で 150 行すべて NULL だが、結合はこの
        sha256 側で成立するので ③ ↔ ⑤ の辿り直しはできる。

        `core_stocks.id` の解決も**この try の中**に入れてある。外に出すと
        SELECT の失敗が例外としてここを素通りし、このクラスが宣言している
        「True=書けた / False=失敗 / None=対象外」の契約が破れる。
        """
        if not self.enabled:
            return None
        if not self.settings.d1_enabled():
            return None
        try:
            stock_id = financials.resolve_stock_id(
                self.d1, record.code, cache=self._stock_ids
            )
            financials.write(
                self.d1,
                [
                    financials.record_to_row(
                        record,
                        stock_id=stock_id,
                        doc_id=doc_id,
                        raw_sha256=raw_sha256,
                    )
                ],
            )
        except D1Error as exc:
            # 移行前の本番は PK に consolidated が無く、ON CONFLICT が一致する
            # UNIQUE を見つけられずここに来る。**黙って後勝ちさせない**ための
            # 失敗なので、メッセージに移行の要否が読めるようにしておく。
            logger.warning(
                "D1 %s への ③ 書き込みに失敗: %s %s: %s",
                financials.TABLE, record.code, record.fiscal_period_end, exc,
            )
            return False
        return True


__all__ = ["CloudSink"]
