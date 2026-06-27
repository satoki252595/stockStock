"""FastAPI 配信アプリ (端末B で起動し、ローカル PostgreSQL を REST 公開) (DESIGN.md §7)。

起動:
    nix develop -c uv run uvicorn jp_stock_pipeline.local_store.api:app --host 0.0.0.0 --port 8000
    または: nix develop -c uv run python -m jp_stock_pipeline.local_store.api

認証: X-API-Key ヘッダ (.env の LOCAL_API_KEY) と照合。未設定なら認証なしで起動し
warning を出す（信頼できる LAN 内のみで運用すること）。

接続はリクエスト毎に open/close するシンプル方式（個人用途で十分。将来は接続プール）。
全クエリはプレースホルダ (%(name)s) を用い、文字列連結しない（SQL インジェクション対策）。
公開対象はローカルにミラー済みの ①〜⑤⑦ と、ローカル専用の ⑧ XBRLファクト。
値の改変はせず読み取り専用 (§3-3)。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from ..config import DB_TARGET_LAN, LocalStoreSettings, load_settings

logger = logging.getLogger(__name__)

# 公開する全テーブル（読み取り専用）と既定の並び順
_MAX_LIMIT = 5000


def _make_auth(settings: LocalStoreSettings):
    """X-API-Key 検証の依存性を返す。api_key 未設定なら認証なし（警告）。"""
    if not settings.api_key:
        logger.warning(
            "LOCAL_API_KEY 未設定: API 認証なしで起動します（信頼できる LAN 内のみで使用）"
        )

    def verify(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
        if settings.api_key and x_api_key != settings.api_key:
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    return verify


def _query(settings: LocalStoreSettings, sql: str, params: dict | None = None) -> list[dict[str, Any]]:
    """読み取りクエリ。接続不能・SQL エラーは 503 で返す。"""
    import psycopg
    from psycopg.rows import dict_row

    try:
        # API は端末B 内で動くため lan プロファイル(既定 localhost)で自DBに接続する
        with psycopg.connect(
            **settings.connect_kwargs(DB_TARGET_LAN), connect_timeout=5, row_factory=dict_row
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or {})
                return cur.fetchall()
    except psycopg.OperationalError as exc:
        logger.error("ローカル DB 接続不可: %s", exc)
        raise HTTPException(status_code=503, detail="local database unavailable") from exc


def _search_text_blocks(
    settings: LocalStoreSettings, q: str, code: str | None, limit: int
) -> list[dict[str, Any]]:
    """定性 textBlock の全文検索。PGroonga があれば &@~、無ければ ILIKE。

    1接続内で PGroonga を試し、演算子未定義（拡張なし）なら rollback して ILIKE で
    再実行する。接続不可は 503。
    """
    import psycopg
    from psycopg.rows import dict_row

    select = (
        "SELECT doc_id, code, element, period_end, value, source, license_tag "
        "FROM xbrl_facts WHERE is_text_block"
    )
    cond = ""
    base_params: dict[str, Any] = {"limit": limit}
    if code:
        cond = " AND code = %(code)s"
        base_params["code"] = code
    pgroonga_sql = f"{select}{cond} AND value &@~ %(q)s ORDER BY doc_id LIMIT %(limit)s"
    ilike_sql = f"{select}{cond} AND value ILIKE %(like)s ORDER BY doc_id LIMIT %(limit)s"
    try:
        with psycopg.connect(
            **settings.connect_kwargs(DB_TARGET_LAN), connect_timeout=5, row_factory=dict_row
        ) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(pgroonga_sql, {**base_params, "q": q})
                    return cur.fetchall()
                except psycopg.OperationalError:
                    raise  # 接続障害は縮退させず外側で 503 に変換する
                except psycopg.Error:
                    # PGroonga 拡張なし(演算子未定義)・PGroonga クエリ構文エラー等は
                    # 同接続を rollback して ILIKE 検索へ安全に縮退する（q は値渡しで注入不可）
                    conn.rollback()
                    cur.execute(ilike_sql, {**base_params, "like": f"%{q}%"})
                    return cur.fetchall()
    except psycopg.OperationalError as exc:
        logger.error("ローカル DB 接続不可: %s", exc)
        raise HTTPException(status_code=503, detail="local database unavailable") from exc


def create_app(settings: LocalStoreSettings | None = None) -> FastAPI:
    settings = settings or load_settings().local_store
    auth = _make_auth(settings)
    app = FastAPI(
        title="JP Stock ローカル API",
        version="0.1.0",
        description=(
            "Notion ①〜⑦ をミラー＋ローカル専用⑧XBRLファクトのローカル PostgreSQL "
            "REST 配信（読み取り専用）"
        ),
    )
    secured = [Depends(auth)]

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        """死活確認（認証不要・DB 接続不要）。"""
        return {"status": "ok"}

    @app.get("/stocks", dependencies=secured, tags=["① 銘柄マスタ"])
    def list_stocks(
        listed: bool | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM stock_master"
        params: dict[str, Any] = {}
        if listed is not None:
            sql += " WHERE listed = %(listed)s"
            params["listed"] = listed
        sql += " ORDER BY code LIMIT %(limit)s OFFSET %(offset)s"
        params.update(limit=limit, offset=offset)
        return _query(settings, sql, params)

    @app.get("/stocks/{code}", dependencies=secured, tags=["① 銘柄マスタ"])
    def get_stock(code: str) -> dict[str, Any]:
        rows = _query(settings, "SELECT * FROM stock_master WHERE code = %(code)s", {"code": code})
        if not rows:
            raise HTTPException(status_code=404, detail=f"銘柄 {code} は未登録")
        return rows[0]

    @app.get("/prices/{code}", dependencies=secured, tags=["② 株価テクニカル"])
    def get_prices(
        code: str,
        date_from: date | None = Query(None, alias="from"),
        date_to: date | None = Query(None, alias="to"),
        limit: int = Query(500, ge=1, le=_MAX_LIMIT),
    ) -> list[dict[str, Any]]:
        """時系列（data_date 降順）。from/to で期間絞り込み。"""
        sql = "SELECT * FROM prices WHERE code = %(code)s"
        params: dict[str, Any] = {"code": code}
        if date_from is not None:
            sql += " AND data_date >= %(date_from)s"
            params["date_from"] = date_from
        if date_to is not None:
            sql += " AND data_date <= %(date_to)s"
            params["date_to"] = date_to
        sql += " ORDER BY data_date DESC LIMIT %(limit)s"
        params["limit"] = limit
        return _query(settings, sql, params)

    @app.get("/financials/{code}", dependencies=secured, tags=["③ 財務サマリ"])
    def get_financials(
        code: str,
        disclosure_type: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM financials WHERE code = %(code)s"
        params: dict[str, Any] = {"code": code}
        if disclosure_type:
            sql += " AND disclosure_type = %(disclosure_type)s"
            params["disclosure_type"] = disclosure_type
        sql += " ORDER BY fiscal_period_end DESC LIMIT %(limit)s"
        params["limit"] = limit
        return _query(settings, sql, params)

    @app.get("/disclosures", dependencies=secured, tags=["④ 開示書類"])
    def list_disclosures(
        code: str | None = None,
        doc_type: str | None = None,
        date_from: datetime | None = Query(None, alias="from"),
        date_to: datetime | None = Query(None, alias="to"),
        limit: int = Query(200, ge=1, le=_MAX_LIMIT),
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM disclosures WHERE TRUE"
        params: dict[str, Any] = {}
        if code:
            sql += " AND code = %(code)s"
            params["code"] = code
        if doc_type:
            sql += " AND doc_type = %(doc_type)s"
            params["doc_type"] = doc_type
        if date_from is not None:
            sql += " AND disclosed_at >= %(date_from)s"
            params["date_from"] = date_from
        if date_to is not None:
            sql += " AND disclosed_at <= %(date_to)s"
            params["date_to"] = date_to
        sql += " ORDER BY disclosed_at DESC LIMIT %(limit)s"
        params["limit"] = limit
        return _query(settings, sql, params)

    @app.get("/raw", dependencies=secured, tags=["⑤ 原本ファイル"])
    def list_raw(
        scope: str | None = None,
        limit: int = Query(200, ge=1, le=_MAX_LIMIT),
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM raw_files"
        params: dict[str, Any] = {}
        if scope:
            sql += " WHERE scope = %(scope)s"
            params["scope"] = scope
        sql += " ORDER BY fetched_at DESC LIMIT %(limit)s"
        params["limit"] = limit
        return _query(settings, sql, params)

    @app.get("/facts", dependencies=secured, tags=["⑧ XBRLファクト"])
    def list_facts(
        doc_id: str | None = None,
        code: str | None = None,
        element: str | None = None,
        text_only: bool = False,
        limit: int = Query(200, ge=1, le=_MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, Any]]:
        """XBRL 全ファクト（数値＋定性 textBlock）。doc_id/code/element で絞り込み。

        text_only=true で定性 textBlock のみ（is_text_block）。element は前方一致。
        """
        sql = "SELECT * FROM xbrl_facts WHERE TRUE"
        params: dict[str, Any] = {}
        if doc_id:
            sql += " AND doc_id = %(doc_id)s"
            params["doc_id"] = doc_id
        if code:
            sql += " AND code = %(code)s"
            params["code"] = code
        if element:
            sql += " AND element LIKE %(element)s"
            params["element"] = f"{element}%"
        if text_only:
            sql += " AND is_text_block"
        sql += " ORDER BY doc_id, element LIMIT %(limit)s OFFSET %(offset)s"
        params.update(limit=limit, offset=offset)
        return _query(settings, sql, params)

    @app.get("/facts/search", dependencies=secured, tags=["⑧ XBRLファクト"])
    def search_facts(
        q: str = Query(..., min_length=1, description="検索語（定性 textBlock 全文検索）"),
        code: str | None = None,
        limit: int = Query(100, ge=1, le=_MAX_LIMIT),
    ) -> list[dict[str, Any]]:
        """定性 textBlock の日本語全文検索。

        PGroonga 拡張があれば全文検索演算子 `&@~`、無ければ ILIKE へ自動フォールバック。
        factual-cite（短信原文）も含むため、公開用途では license_tag で要フィルタ。
        """
        return _search_text_blocks(settings, q, code, limit)

    @app.get("/jobs", dependencies=secured, tags=["⑦ 収集ジョブログ"])
    def list_jobs(
        job_name: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM job_log"
        params: dict[str, Any] = {}
        if job_name:
            sql += " WHERE job_name = %(job_name)s"
            params["job_name"] = job_name
        sql += " ORDER BY finished_at DESC LIMIT %(limit)s"
        params["limit"] = limit
        return _query(settings, sql, params)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = load_settings().local_store
    host = os.environ.get("LOCAL_API_HOST", "0.0.0.0")
    port = int(os.environ.get("LOCAL_API_PORT", "8000"))
    uvicorn.run(create_app(settings), host=host, port=port)


if __name__ == "__main__":
    main()
