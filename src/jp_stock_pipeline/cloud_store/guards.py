"""mutable な R2 JSON への PUT 前ガード (docs/CF-CANONICAL-DESIGN.md §2.1-2.2)。

R2 にはオブジェクトバージョニングが無いため、全置換 PUT で内容が縮むと復元できない。
特に ``margin/weeks.json`` は公開 Worker の信用残 API の**唯一の入口**で、空配列で
上書きすると R2 上にオブジェクトが残っていても画面上はデータが消えたのと同じになる。
既存 10 週のうち 2026-06-12〜07-31 は JPX から再取得できない。

ガードは「1 つでも満たさなければ PUT せず失敗として記録する」(§3-2 欠損を隠さない)。
"""

from __future__ import annotations

from typing import Any

# トップレベルに必ず置く writer 名のキー。R2 の 429 は同一キー競合でしか出ず、
# daily/ (4,445キー) や supply/ (4,351キー) のようにキーが分散する経路では
# writer の二重稼働を検知できない。したがって payload 内の宣言で突き合わせる。
WRITER_KEY = "writer"


class GuardError(RuntimeError):
    """PUT を拒否した。呼び出し側は取得単位の失敗として記録する。"""


def _arrays(payload: Any, prefix: str = "") -> dict[str, list]:
    """payload に含まれる配列を「パス -> 配列」で平坦に集める。

    ``bars`` / ``splits`` / ``series.*`` / ``entries`` のように配列の置き場所は
    プレフィックスごとに違うため、キー名を列挙せず構造から拾う。
    """
    found: dict[str, list] = {}
    if isinstance(payload, list):
        found[prefix or "$"] = payload
        return found
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, list):
                found[path] = value
            elif isinstance(value, dict):
                found.update(_arrays(value, path))
    return found


def _hashable(item: Any) -> Any:
    """配列要素を集合比較できる形にする（dict は決定的な文字列へ）。"""
    if isinstance(item, (dict, list)):
        import json  # noqa: PLC0415 - 例外的な要素だけで使う

        return json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
    return item


def check_contract_keys(payload: Any, contract: dict[str, tuple[str, ...]]) -> None:
    """契約キー（削除・改名が禁止されたキー）が揃っているかを検証する。

    contract は {"$": ("code", "updated"), "bars[]": ("date", "o", ...)} の形。
    "$" はトップレベル、"名前[]" はその配列の各要素に必須のキー。
    """
    for path, required in contract.items():
        if path == "$":
            if not isinstance(payload, dict):
                raise GuardError("契約違反: トップレベルが object ではない")
            missing = [k for k in required if k not in payload]
            if missing:
                raise GuardError(f"契約違反: トップレベルに {missing} が無い")
            continue
        name = path.removesuffix("[]")
        rows = payload.get(name) if isinstance(payload, dict) else None
        if rows is None:
            raise GuardError(f"契約違反: 配列 {name} が無い")
        if not isinstance(rows, list):
            raise GuardError(f"契約違反: {name} が配列ではない")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise GuardError(f"契約違反: {name}[{index}] が object ではない")
            missing = [k for k in required if k not in row]
            if missing:
                raise GuardError(f"契約違反: {name}[{index}] に {missing} が無い")


def check_no_regression(
    old: Any | None,
    new: Any,
    *,
    writer: str,
    contract: dict[str, tuple[str, ...]] | None = None,
    require_writer: bool = True,
) -> None:
    """後退禁止ガード。old=None は新規作成（GET が 404 だった場合のみ許される）。

    呼び出し側は「GET が 404 以外のエラーで失敗したら old を渡さず PUT もしない」
    ことを守る（ガード1。「無かったこと」にしないため）。
    """
    if require_writer and isinstance(new, dict) and new.get(WRITER_KEY) != writer:
        raise GuardError(
            f"ガード5違反: 書こうとしている payload の {WRITER_KEY}="
            f"{(new.get(WRITER_KEY) if isinstance(new, dict) else None)!r} が自分({writer!r})と違う"
        )
    if contract:
        check_contract_keys(new, contract)

    if old is None:
        return

    if require_writer and isinstance(old, dict):
        previous = old.get(WRITER_KEY)
        if previous is not None and previous != writer:
            raise GuardError(
                f"ガード5違反: 既存オブジェクトの {WRITER_KEY}={previous!r} が自分({writer!r})と違う。"
                " writer が二重稼働している可能性がある"
            )

    old_arrays = _arrays(old)
    new_arrays = _arrays(new)
    for path, old_items in old_arrays.items():
        new_items = new_arrays.get(path)
        if new_items is None:
            raise GuardError(f"ガード2違反: 配列 {path} が消えた（{len(old_items)} 要素）")
        if len(new_items) < len(old_items):
            raise GuardError(
                f"ガード2違反: 配列 {path} の要素が {len(old_items)} -> {len(new_items)} へ減った"
            )
        old_set = {_hashable(x) for x in old_items}
        new_set = {_hashable(x) for x in new_items}
        lost = old_set - new_set
        if lost:
            sample = sorted(str(x) for x in lost)[:3]
            raise GuardError(
                f"ガード3違反: 配列 {path} の既存要素 {len(lost)} 件が新配列に無い（例: {sample}）"
            )

    if isinstance(old, dict) and isinstance(new, dict):
        old_first = old.get("first_date")
        new_first = new.get("first_date")
        if old_first and new_first and str(new_first) > str(old_first):
            raise GuardError(
                f"ガード4違反: first_date が後退した {old_first!r} -> {new_first!r}"
            )
