from __future__ import annotations

from typing import Any


def normalize_benchmark_data_update_entry_aliases(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    if "dst_path" not in normalized and isinstance(normalized.get("path"), str):
        normalized["dst_path"] = normalized["path"]
    if "url" not in normalized and isinstance(normalized.get("source"), str):
        normalized["url"] = normalized.pop("source")
    data_source = normalized.pop("data_source", None)
    if isinstance(data_source, dict):
        if "url" not in normalized and isinstance(data_source.get("url"), str):
            normalized["url"] = data_source["url"]
        if "save_method" not in normalized and isinstance(data_source.get("save_method"), str):
            normalized["save_method"] = data_source["save_method"]
    return normalized
