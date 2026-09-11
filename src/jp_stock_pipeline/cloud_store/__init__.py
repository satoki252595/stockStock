"""Cloudflare 正本ストア (R2 + D1)。

配置の原則 (docs/CF-CANONICAL-DESIGN.md):
- **R2** = 長期時系列・⑤原本・大容量派生。
- **D1** = 断面・索引・イベント行。次の3条件を**すべて**満たすものだけ置く。
  (1) 年間増加 10万行以下 (2) 索引でカバーされる述語だけで引ける (3) 1行 2MB 未満。
  1つでも欠けたら R2 へ落とす（D1 の 1DB 10GB 上限は申請でも引き上げ不可、
  かつ課金軸が走査行数のため）。

このパッケージは書き込み経路だけを提供し、どのデータをどこへ置くかは
jobs/ 側が決める。
"""

from .guards import GuardError, check_contract_keys, check_no_regression
from .keys import derived_key, margin_key, raw_key, supply_key

__all__ = [
    "GuardError",
    "check_contract_keys",
    "check_no_regression",
    "derived_key",
    "margin_key",
    "raw_key",
    "supply_key",
]
