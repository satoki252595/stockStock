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

# D1 の compound SELECT（UNION / UNION ALL / INTERSECT / EXCEPT）の項数上限。
# 素の SQLite の既定は 500 だが、D1 は **5** で、6 項目から
# `too many terms in compound SELECT: SQLITE_ERROR` を返す（2026-09-12 に本番で実測）。
# 複数表を1文で数えるようなクエリはここで分割する。
MAX_COMPOUND_SELECT_TERMS = 5


class D1Error(RuntimeError):
    """D1 への読み書きに失敗した（取得単位の失敗として記録する）。"""


class D1Store:
    def __init__(
        self,
        settings: CloudStoreSettings,
        *,
        writer: str,
        database_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.writer = writer
        # 既定は正本 DB。移行元 (kabulab-cf) を読むときだけ差し替える。
        self.database_id = database_id or settings.d1_database_id

    @property
    def _url(self) -> str:
        return (
            f"https://api.cloudflare.com/client/v4/accounts/{self.settings.cf_account_id}"
            f"/d1/database/{self.database_id}/query"
        )

    def query(
        self, sql: str, params: list | None = None, *, idempotent: bool = True
    ) -> list[dict[str, Any]]:
        """1 文を実行して結果行を返す。

        Cloudflare の API は HTTP 200 でも body の success=false でエラーを返すため、
        ステータスコードだけで成否を判断しない（EDINET と同じ落とし穴）。

        `idempotent=False` は**再送してはいけない文**に使う。既定の再送経路は
        タイムアウト・5xx・429 で最大3回まで自動で投げ直すため、`ALTER TABLE
        ADD COLUMN` のような非冪等な DDL では「D1 側は成功したが応答が失われ、
        再送が `duplicate column name` を返す」= 実際は適用済みなのに失敗として
        報告される、が起こりうる。
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
                # upsert と SELECT は再送しても二重作成しない。DDL だけは
                # 呼び出し側が idempotent=False を渡して再送を止める。
                idempotent=idempotent,
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

    def rows_per_request(self, column_count: int) -> int:
        """1 リクエストに詰められる行数。バインドパラメータ上限から逆算する。

        D1 の 1 リクエストのバインドパラメータ上限は 100。14 列なら 7 行入る。
        1 行ずつ投げると往復回数が行数と同じになり、4,755 行で約 16 分かかった
        （本番実測）。まとめることで往復を 1/7 に減らす。
        """
        if column_count <= 0:
            raise D1Error("列が空")
        if column_count > MAX_BOUND_PARAMS:
            raise D1Error(f"列数が D1 のバインド上限を超える: {column_count}")
        return max(1, MAX_BOUND_PARAMS // column_count)

    def upsert(
        self,
        table: str,
        columns: list[str],
        rows: list[list],
        *,
        conflict: list[str],
        keep: list[str] | None = None,
    ) -> int:
        """ON CONFLICT DO UPDATE の upsert。複数行を 1 文にまとめて往復を減らす。

        更新する列は conflict に含まれないものだけ（主キーを自分で上書きしない）。
        途中のチャンクで失敗したら、そこで例外を投げて止める。既に適用済みの
        チャンクは残るが、upsert なので再実行で収束する（部分適用を隠さない §3-2）。

        `keep` に挙げた列は conflict 時に更新しない（`{table}.{col}` で既存値を
        そのまま書き戻す）。取得のたびの再送で「初回取得時刻」のような来歴列が
        `excluded.*` で毎回上書きされるのを防ぐために使う。SQLite の upsert では
        テーブル名で修飾した列参照が更新前の既存値を指す（`excluded.col` は
        逆に挿入しようとした側の値を指す）。
        """
        if not rows:
            return 0
        width = len(columns)
        for index, row in enumerate(rows):
            if len(row) != width:
                raise D1Error(
                    f"{index} 行目の値の数が列数と違う: {len(row)} != {width}"
                )
        chunk_size = self.rows_per_request(width)
        one = "(" + ", ".join("?" for _ in columns) + ")"
        keep_set = set(keep or ())
        updates = ", ".join(
            f"{c} = {table}.{c}" if c in keep_set else f"{c} = excluded.{c}"
            for c in columns
            if c not in conflict
        )
        if not updates:
            raise D1Error(
                f"更新できる列が無い（全列が conflict キー）: {table} {columns}"
            )
        written = 0
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            sql = (
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES {', '.join(one for _ in chunk)} "
                f"ON CONFLICT ({', '.join(conflict)}) DO UPDATE SET {updates}"
            )
            params: list = []
            for row in chunk:
                params.extend(row)
            self.query(sql, params)
            written += len(chunk)
        return written


__all__ = ["D1Error", "D1Store", "MAX_BOUND_PARAMS", "MAX_COMPOUND_SELECT_TERMS"]
