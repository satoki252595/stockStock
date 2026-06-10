"""変換レイヤー (§5.2 値不変)。共通入口 convert_artifact を再エクスポートする。"""

from .json_to_parquet import convert_artifact

__all__ = ["convert_artifact"]
