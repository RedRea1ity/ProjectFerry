"""Extract and restore translatable text in JSON rich-text components."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ferrycore.models import Entry


def extract_text_nodes(value: Any, pointer: str = "") -> list[tuple[str, str]]:
    """Return component `text` strings while leaving all structural fields untouched."""
    result: list[tuple[str, str]] = []
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            result.append((f"{pointer}/text", text))
        elif isinstance(text, list):
            result.extend(extract_text_nodes(text, f"{pointer}/text"))
        for key in ("extra", "with"):
            if key in value:
                child = value[key]
                escaped = key.replace("~", "~0").replace("/", "~1")
                result.extend(extract_text_nodes(child, f"{pointer}/{escaped}"))
        hover = value.get("hoverEvent")
        if isinstance(hover, dict) and hover.get("action") == "show_text":
            field = "contents" if "contents" in hover else "value" if "value" in hover else None
            if field:
                result.extend(extract_text_nodes(hover[field], f"{pointer}/hoverEvent/{field}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(extract_text_nodes(child, f"{pointer}/{index}"))
    elif isinstance(value, str) and value.strip():
        result.append((pointer, value))
    return result


def component_entries(modid: str, values: dict[str, Any], source: str) -> list[Entry]:
    entries: list[Entry] = []
    for lang_key, value in values.items():
        if isinstance(value, (dict, list)):
            for pointer, text in extract_text_nodes(value):
                if text.strip():
                    synthetic = f"ftblocalizer:{lang_key}#{pointer}"
                    entries.append(Entry(modid, synthetic, text, source, "ftb-component", rel_path=lang_key, json_pointer=pointer))
    return entries


def translated_components(values: dict[str, Any], entries: list[Entry], translations: dict[str, dict[str, str]], modid: str, prior: dict[str, Any] | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {}
    by_key: dict[str, dict[str, str]] = {}
    for entry in entries:
        value = translations.get(modid, {}).get(entry.key)
        if value is not None:
            by_key.setdefault(entry.rel_path, {})[entry.json_pointer] = value
    for key, replacements in by_key.items():
        base = (prior or {}).get(key, values[key])
        output[key] = apply_text_nodes(base, replacements)
    return output


def apply_text_nodes(value: Any, translations: dict[str, str], pointer: str = "") -> Any:
    """Copy a JSON component and replace only `text` nodes addressed by pointers."""
    result = deepcopy(value)
    if isinstance(result, dict):
        text_path = f"{pointer}/text"
        if isinstance(result.get("text"), str) and text_path in translations:
            result["text"] = translations[text_path]
        elif isinstance(result.get("text"), list):
            result["text"] = apply_text_nodes(result["text"], translations, text_path)
        for key in ("extra", "with"):
            if key in result:
                escaped = key.replace("~", "~0").replace("/", "~1")
                result[key] = apply_text_nodes(result[key], translations, f"{pointer}/{escaped}")
        hover = result.get("hoverEvent")
        if isinstance(hover, dict) and hover.get("action") == "show_text":
            field = "contents" if "contents" in hover else "value" if "value" in hover else None
            if field:
                hover[field] = apply_text_nodes(hover[field], translations, f"{pointer}/hoverEvent/{field}")
    elif isinstance(result, list):
        for index, child in enumerate(result):
            result[index] = apply_text_nodes(child, translations, f"{pointer}/{index}")
    elif isinstance(result, str) and pointer in translations:
        result = translations[pointer]
    return result
