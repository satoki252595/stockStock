"""端末B(ローカル PostgreSQL)への dual-write と FastAPI 配信 (DESIGN.md §7)。

Notion を正本としつつ、収集パイプラインが同時にローカル PostgreSQL へ
ミラー書き込みし、ユーザーが FastAPI(REST + APIキー)で逐次アクセスできる
ようにする。接続情報は全て .env (LOCAL_DB_*) で管理し、コードに埋め込まない。

- schema:  ①〜⑤⑦ に対応するテーブル DDL（②株価は (code, data_date) 時系列）
- mappers: 正規化レコード → (SQL, params) の純粋関数（テスト可能）
- sink:    LocalStore（接続・スキーマ初期化・UPSERT 実行）
- api:     FastAPI アプリ（端末B で起動）
"""

from __future__ import annotations

from .sink import LocalStore, connect_local_store

__all__ = ["LocalStore", "connect_local_store"]
