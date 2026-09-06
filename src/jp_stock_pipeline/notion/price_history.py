"""①銘柄ページ配下の株価テクニカル履歴子DB (日次追記・8,000行でシャード)。

②は最新スナップショットのまま残し、計算済みテクニカルとバリュエーションの
その日時点値は銘柄ページの子DBへ蓄積する。キー=基準日 (title equals)。
master_sync はポインタ列を触らない（本モジュールと prices_daily が所有）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date

from ..config import Settings
from ..models import PriceTechnicalRecord
from . import schema as S
from .client import NotionClient
from .upsert import (
    _PRICE_FIELD_TO_PROP,
    _find_page,
    _set,
    number_prop,
    provenance_properties,
    real_page_id,
    relation_prop,
    text_prop,
    title_prop,
)

logger = logging.getLogger(__name__)

_HISTORY_TITLE_RE = re.compile(
    rf"^{re.escape(S.HISTORY_DB_TITLE)}(?:_(\d+))?$"
)


@dataclass
class StockMasterState:
    """① 1行の page_id と履歴子DBポインタ。"""

    page_id: str
    history_db_id: str | None = None
    history_shard: int = 1
    history_row_count: int = 0


def history_db_title(shard: int) -> str:
    """シャード1は無印、2以降は _N。"""
    if shard <= 1:
        return S.HISTORY_DB_TITLE
    return f"{S.HISTORY_DB_TITLE}_{shard}"


def parse_history_db_title(title: str) -> int | None:
    """子DBタイトルからシャード番号を読む。不一致は None。"""
    match = _HISTORY_TITLE_RE.match(title.strip())
    if not match:
        return None
    return int(match.group(1)) if match.group(1) else 1


def should_roll_shard(
    row_count: int, *, threshold: int = S.HISTORY_SHARD_THRESHOLD
) -> bool:
    return row_count >= threshold


def price_history_filter(data_date: date) -> dict:
    """履歴キー = 基準日 (title equals)。"""
    return {
        "property": S.HISTORY_PROP_DATE_TITLE,
        "title": {"equals": data_date.isoformat()},
    }


def price_history_properties(
    record: PriceTechnicalRecord,
    extra_raw_page_ids: Iterable[str] | None = None,
) -> dict:
    """履歴1行。title はデータ基準日。銘柄マスタ relation は持たない。"""
    data_date = record.provenance.data_date
    if data_date is None:
        raise ValueError("履歴行はデータ基準日が必須")
    props = {
        S.HISTORY_PROP_DATE_TITLE: title_prop(data_date.isoformat()),
        **provenance_properties(record.provenance),
    }
    for field_name, prop_name in _PRICE_FIELD_TO_PROP.items():
        _set(props, prop_name, number_prop, getattr(record, field_name))
    extra_ids = [rid for rid in (extra_raw_page_ids or []) if real_page_id(rid)]
    if extra_ids:
        existing = props.get(S.PROP_RAW_RELATION, {"relation": []})["relation"]
        ids = [item["id"] for item in existing] + extra_ids
        props[S.PROP_RAW_RELATION] = relation_prop(dict.fromkeys(ids))
    return props


def _plain_text(prop: dict | None) -> str:
    if not prop:
        return ""
    rich = prop.get("rich_text") or []
    if not rich:
        return ""
    return (rich[0].get("plain_text") or "").strip()


def _int_number(prop: dict | None, default: int) -> int:
    if not prop:
        return default
    value = prop.get("number")
    if value is None:
        return default
    return int(value)


def parse_master_state(page: dict) -> tuple[str, StockMasterState] | None:
    """① ページから (銘柄コード, state) を読む。コードが空なら None。"""
    props = page.get("properties") or {}
    rich = (props.get(S.MASTER_PROP_CODE) or {}).get("rich_text") or []
    if not rich:
        return None
    code = (rich[0].get("plain_text") or "").strip()
    if not code:
        return None
    shard = _int_number(props.get(S.MASTER_PROP_HISTORY_SHARD), 1)
    if shard < 1:
        shard = 1
    return code, StockMasterState(
        page_id=page["id"],
        history_db_id=_plain_text(props.get(S.MASTER_PROP_HISTORY_DB_ID)) or None,
        history_shard=shard,
        history_row_count=max(0, _int_number(props.get(S.MASTER_PROP_HISTORY_ROW_COUNT), 0)),
    )


def load_stock_master_state(
    client: NotionClient, settings: Settings
) -> dict[str, StockMasterState]:
    """① 全行の {銘柄コード: StockMasterState}。履歴ポインタも含む。"""
    pages = client.query_database(settings.db_id("stock_master"))
    out: dict[str, StockMasterState] = {}
    for page in pages:
        parsed = parse_master_state(page)
        if parsed:
            out[parsed[0]] = parsed[1]
    return out


def ensure_master_history_properties(client: NotionClient, settings: Settings) -> None:
    """既存①へ履歴ポインタ列が無ければ追加する（型変更はしない）。"""
    if client.dry_run:
        return
    db_id = settings.db_id("stock_master")
    if str(db_id).startswith("dry-run"):
        return
    existing = client.retrieve_database(db_id).get("properties") or {}
    missing = S.missing_properties(S.history_pointer_properties_schema(), existing)
    if not missing:
        return
    client.update_database(db_id, properties=missing)
    logger.info("① へ履歴ポインタ列を追加: %s", sorted(missing))


def write_history_pointer(client: NotionClient, state: StockMasterState) -> None:
    """① のポインタ3列だけ部分更新する（他列は触らない）。"""
    client.update_page(
        state.page_id,
        {
            S.MASTER_PROP_HISTORY_DB_ID: text_prop(state.history_db_id),
            S.MASTER_PROP_HISTORY_SHARD: number_prop(state.history_shard),
            S.MASTER_PROP_HISTORY_ROW_COUNT: number_prop(state.history_row_count),
        },
    )


def _count_database_rows(client: NotionClient, database_id: str) -> int:
    return len(client.query_database(database_id))


def _find_latest_history_child(
    client: NotionClient, master_page_id: str
) -> tuple[str, int] | None:
    """銘柄ページ直下の履歴子DBのうち、最大シャードを返す。"""
    found: list[tuple[str, int]] = []
    for block in client.list_child_blocks(master_page_id):
        if block.get("type") != "child_database":
            continue
        title = (block.get("child_database") or {}).get("title") or ""
        shard = parse_history_db_title(title)
        if shard is not None:
            found.append((block["id"], shard))
    if not found:
        return None
    return max(found, key=lambda item: item[1])


def _create_history_db(
    client: NotionClient, settings: Settings, master_page_id: str, shard: int
) -> str:
    raw_db_id = settings.db_id("raw_files")
    resp = client.create_database(
        parent_page_id=master_page_id,
        title=history_db_title(shard),
        properties=S.history_prices_schema(raw_db_id),
        description=(
            f"{S.HISTORY_DB_TITLE} シャード{shard}。"
            f"{S.HISTORY_SHARD_THRESHOLD}行で次シャードを作る。"
        ),
    )
    logger.info(
        "履歴子DB作成: page=%s shard=%s id=%s", master_page_id, shard, resp["id"]
    )
    return resp["id"]


def ensure_price_history_database(
    client: NotionClient,
    settings: Settings,
    state: StockMasterState,
) -> StockMasterState:
    """現行シャードを返す。未作成なら作り、行数閾値なら次シャードを切る。"""
    if state.history_db_id and should_roll_shard(state.history_row_count):
        next_shard = state.history_shard + 1
        db_id = _create_history_db(client, settings, state.page_id, next_shard)
        new_state = replace(
            state, history_db_id=db_id, history_shard=next_shard, history_row_count=0
        )
        write_history_pointer(client, new_state)
        return new_state
    if state.history_db_id:
        return state

    discovered = _find_latest_history_child(client, state.page_id)
    if discovered:
        db_id, shard = discovered
        count = _count_database_rows(client, db_id)
        new_state = replace(
            state, history_db_id=db_id, history_shard=shard, history_row_count=count
        )
        write_history_pointer(client, new_state)
        if should_roll_shard(count):
            return ensure_price_history_database(client, settings, new_state)
        return new_state

    db_id = _create_history_db(client, settings, state.page_id, 1)
    new_state = replace(state, history_db_id=db_id, history_shard=1, history_row_count=0)
    write_history_pointer(client, new_state)
    return new_state


def upsert_price_history(
    client: NotionClient,
    settings: Settings,
    record: PriceTechnicalRecord,
    state: StockMasterState,
    extra_raw_page_ids: Iterable[str] | None = None,
) -> tuple[str, StockMasterState, bool]:
    """履歴子DBへ冪等 upsert。戻り値=(page_id, 更新後state, 新規作成したか)。"""
    state = ensure_price_history_database(client, settings, state)
    if not state.history_db_id:
        raise RuntimeError("履歴子DB ID を確保できない")
    props = price_history_properties(record, extra_raw_page_ids)
    data_date = record.provenance.data_date
    if data_date is None:
        raise ValueError("履歴行はデータ基準日が必須")
    existing = _find_page(client, state.history_db_id, price_history_filter(data_date))
    if existing:
        client.update_page(existing, props)
        return existing, state, False
    page_id = client.create_page(
        parent={"database_id": state.history_db_id}, properties=props
    )["id"]
    new_state = replace(state, history_row_count=state.history_row_count + 1)
    write_history_pointer(client, new_state)
    return page_id, new_state, True
