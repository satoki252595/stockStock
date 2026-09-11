"""D1 への書き込み (Cloudflare REST API)。

D1 に置いてよいのは次の3条件を**すべて**満たすものだけ (docs/CF-CANONICAL-DESIGN.md):
(1) 年間増加 10万行以下 (2) 索引でカバーされる述語だけで引ける (3) 1行 2MB 未満。

D1 の課金軸は**走査行数**であり LIMIT では下がらない。したがって外部へ出す述語は
索引でカバーされるものだけに限り、D1 の API トークンは外部に配らない (§D)。
"""

from __future__ import annotations

import logging
from typing import Any

from .. import http
from ..config import CloudStoreSettings

logger = logging.getLogger(__name__)

# D1 の 1 リクエストのバインドパラメータ上限は 100。バッチの分割単位はこの
# 上限から逆算する（列数 × 行数 <= 100）。超えると実行時に落ちる。
MAX_BOUND_PARAMS = 100


class D1Error(RuntimeError):
    """D1 への読み書きに失敗した（取得単位の失敗として記録する）。"""


class D1Store:
    def __init__(self, settings: CloudStoreSettings, *, writer: str) -> None:
        self.settings = settings
        self.writer = writer

    @property
    def _url(self) -> str:
        return (
            f"https://api.cloudflare.com/client/v4/accounts/{self.settings.cf_account_id}"
            f"/d1/database/{self.settings.d1_database_id}/query"
        )

    def query(self, sql: str, params: list | None = None) -> list[dict[str, Any]]:
        """1 文を実行して結果行を返す。

        Cloudflare の API は HTTP 200 でも body の success=false でエラーを返すため、
        ステータスコードだけで成否を判断しない（EDINET と同じ落とし穴）。
        """
        if params and len(params) > MAX_BOUND_PARAMS:
            raise D1Error(
                f"D1 のバインドパラメータ上限 {MAX_BOUND_PARAMS} 超過: {len(params)} 個"
            )
        try:
            resp = http.post_json(
                self._url,
                json_body={"sql": sql, "params": params or []},
                headers={"Authorization": f"Bearer {self.settings.cf_api_token}"},
                # upsert と SELECT のみを通す前提なので冪等。再送しても二重作成しない。
                idempotent=True,
            )
        except http.FetchError as exc:
            raise D1Error(f"D1 リクエスト失敗: {exc}") from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise D1Error(f"D1 応答が JSON でない: {sql[:80]!r}") from exc
        if not body.get("success"):
            errors = body.get("errors") or body.get("messages")
            raise D1Error(f"D1 エラー: {errors!r} sql={sql[:120]!r}")
        result = body.get("result") or []
        if not result:
            return []
        return result[0].get("results") or []

    def execute_many(self, sql: str, rows: list[list]) -> int:
        """同一 SQL を行ごとに実行する。書き込んだ行数を返す。

        D1 REST には複数文のバッチがあるが、1 リクエストのバインドパラメータ上限が
        100 なので、行数 × 列数がそれを超えないよう呼び出し側で分割する。ここでは
        1 行ずつ実行し、失敗した行を明示して止める（部分適用を隠さない §3-2）。
        """
        written = 0
        for row in rows:
            self.query(sql, row)
            written += 1
        return written

    def upsert(
        self, table: str, columns: list[str], rows: list[list], *, conflict: list[str]
    ) -> int:
        """ON CONFLICT DO UPDATE の upsert。

        更新する列は conflict に含まれないものだけ（主キーを自分で上書きしない）。
        """
        if not rows:
            return 0
        if len(columns) > MAX_BOUND_PARAMS:
            raise D1Error(f"列数が D1 のバインド上限を超える: {len(columns)}")
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{c} = excluded.{c}" for c in columns if c not in conflict
        )
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET {updates}"
        )
        return self.execute_many(sql, rows)


__all__ = ["D1Error", "D1Store", "MAX_BOUND_PARAMS"]
