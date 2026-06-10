"""変換レイヤー (§5.2 値不変)。共通入口を再エクスポートする。"""

from .json_to_parquet import attach_dataframe_parquet, convert_artifact

__all__ = ["attach_dataframe_parquet", "convert_artifact"]
