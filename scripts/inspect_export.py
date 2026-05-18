"""Inspecciona un export oficial de Claude.ai para entender el shape del JSON.

Read-only. No extrae ni modifica el zip. Imprime listado de archivos, esquema y
estadísticas agregadas. NO imprime contenido real de mensajes (texto se reemplaza
por marcadores tipo `<text:N chars>`) para evitar leakear conversaciones a logs.

Uso:
    uv run python scripts/inspect_export.py [path/al/export.zip]

Si no se pasa path, busca el primer .zip en data/exports/.
"""

from __future__ import annotations

import json
import sys
import zipfile
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Any

DEFAULT_DIR = Path("data/exports")


def find_default_export() -> Path | None:
    if not DEFAULT_DIR.exists():
        return None
    zips = sorted(DEFAULT_DIR.glob("*.zip"))
    return zips[0] if zips else None


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        export_path = Path(argv[1])
    else:
        default = find_default_export()
        if default is None:
            print(
                f"ERROR: no se encontró ningún .zip en {DEFAULT_DIR} y no se pasó path.",
                file=sys.stderr,
            )
            return 1
        export_path = default

    if not export_path.exists():
        print(f"ERROR: export no encontrado en {export_path}", file=sys.stderr)
        return 1

    size = export_path.stat().st_size
    print(f"Inspeccionando: {export_path}")
    print(f"Tamaño zip: {size:,} bytes ({size / 1024 / 1024:.2f} MB)\n")

    with zipfile.ZipFile(export_path) as zf:
        names = zf.namelist()
        print(f"=== Archivos en el zip ({len(names)}) ===")
        for n in names:
            info = zf.getinfo(n)
            print(f"  {n:<50}  {info.file_size:>14,} bytes")

        for name in names:
            if name.endswith(".json"):
                inspect_json(zf, name)

    return 0


def inspect_json(zf: zipfile.ZipFile, name: str) -> None:
    print(f"\n\n=== Estructura: {name} ===")
    with zf.open(name) as f:
        data = json.load(f)

    if isinstance(data, list):
        print(f"Top-level: list con {len(data):,} elementos")
        if data:
            inspect_list_items(data)
    elif isinstance(data, dict):
        print(f"Top-level: dict con keys {sorted(data.keys())}")
        for k, v in data.items():
            print(f"  {k}: {short_type(v)}")
    else:
        print(f"Top-level: {type(data).__name__}")


def short_type(v: Any) -> str:
    if isinstance(v, list):
        return f"list[{len(v)}]"
    if isinstance(v, dict):
        return f"dict (keys: {sorted(v.keys())[:6]}{'...' if len(v) > 6 else ''})"
    if isinstance(v, str):
        return f"str (len={len(v)})"
    return type(v).__name__


def inspect_list_items(items: list[Any]) -> None:
    key_counts: Counter[str] = Counter()
    for item in items:
        if isinstance(item, dict):
            key_counts.update(item.keys())

    total = len(items)
    print(f"\nKeys que aparecen en los items (de {total} totales):")
    for key, count in key_counts.most_common():
        flag = " (no en todos)" if count < total else ""
        print(f"  {key:<32}  {count:>6}{flag}")

    sample = next((it for it in items if isinstance(it, dict)), None)
    if sample is not None:
        print("\nShape de un item de muestra (texto redactado):")
        print_redacted(sample, indent=2)

    msg_key = None
    for candidate in ("chat_messages", "messages"):
        if candidate in key_counts:
            msg_key = candidate
            break
    if msg_key is not None:
        analyze_conversations(items, msg_key)


def print_redacted(obj: Any, indent: int = 0, max_depth: int = 4) -> None:
    pad = " " * indent
    if max_depth <= 0:
        print(f"{pad}...")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                print(f"{pad}{k}: <str:{len(v)} chars>")
            elif isinstance(v, list):
                print(f"{pad}{k}: list[{len(v)}]")
                if v and isinstance(v[0], dict):
                    print(f"{pad}  [0]:")
                    print_redacted(v[0], indent + 4, max_depth - 1)
            elif isinstance(v, dict):
                print(f"{pad}{k}: dict")
                print_redacted(v, indent + 2, max_depth - 1)
            elif v is None:
                print(f"{pad}{k}: None")
            else:
                print(f"{pad}{k}: {type(v).__name__} = {v!r}")
    elif isinstance(obj, list):
        if obj:
            print(f"{pad}list[{len(obj)}], primer elemento:")
            print_redacted(obj[0], indent + 2, max_depth - 1)
        else:
            print(f"{pad}list vacía")
    else:
        print(f"{pad}{type(obj).__name__}")


def analyze_conversations(convs: list[dict[str, Any]], msg_key: str) -> None:
    msg_counts: list[int] = []
    sender_counter: Counter[str] = Counter()
    msg_top_keys: Counter[str] = Counter()
    content_top_keys: Counter[str] = Counter()
    content_types: Counter[str] = Counter()
    attachment_counter: Counter[str] = Counter()
    char_per_msg: list[int] = []  # chars del campo canónico (content si existe, sino text)
    text_field_chars: list[int] = []
    content_field_chars: list[int] = []
    text_mode_counter: Counter[str] = Counter()  # text_only / content_only / both / neither
    chars_per_conv: list[int] = []
    created_samples: list[str] = []

    has_branches = 0
    has_parent_uuid = 0

    for conv in convs:
        msgs = conv.get(msg_key, [])
        if not isinstance(msgs, list):
            continue
        msg_counts.append(len(msgs))
        conv_chars = 0
        for m in msgs:
            if not isinstance(m, dict):
                continue
            msg_top_keys.update(m.keys())
            sender = m.get("sender") or m.get("role")
            if isinstance(sender, str):
                sender_counter[sender] += 1
            if "parent_message_uuid" in m or "parent_uuid" in m:
                has_parent_uuid += 1
            if "branch" in m or "branches" in m:
                has_branches += 1

            text = m.get("text")
            text_len = len(text) if isinstance(text, str) else 0

            content = m.get("content")
            content_len = 0
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        content_top_keys.update(part.keys())
                        ptype = part.get("type")
                        if isinstance(ptype, str):
                            content_types[ptype] += 1
                        ptext = part.get("text")
                        if isinstance(ptext, str):
                            content_len += len(ptext)

            if text_len and content_len:
                text_mode_counter["both"] += 1
            elif text_len:
                text_mode_counter["text_only"] += 1
            elif content_len:
                text_mode_counter["content_only"] += 1
            else:
                text_mode_counter["neither"] += 1

            if text_len:
                text_field_chars.append(text_len)
            if content_len:
                content_field_chars.append(content_len)

            # Campo canónico: content si existe, sino text. Evita doble conteo.
            canonical = content_len if content_len else text_len
            if canonical:
                char_per_msg.append(canonical)
                conv_chars += canonical

            atts = m.get("attachments")
            if isinstance(atts, list) and atts:
                attachment_counter["with_attachments"] += 1
            files = m.get("files")
            if isinstance(files, list) and files:
                attachment_counter["with_files"] += 1

            if len(created_samples) < 3:
                ts = m.get("created_at") or m.get("timestamp")
                if isinstance(ts, str):
                    created_samples.append(ts)

        chars_per_conv.append(conv_chars)

    n = len(convs)
    print(f"\n--- Estadísticas de conversaciones ({n} totales) ---")
    if msg_counts:
        total_msgs = sum(msg_counts)
        print(
            f"Mensajes por conversación: total={total_msgs}, "
            f"min={min(msg_counts)}, max={max(msg_counts)}, "
            f"mean={mean(msg_counts):.1f}, median={median(msg_counts)}"
        )
    print(f"\nKeys de mensajes (sobre {sum(msg_counts)} mensajes totales):")
    for key, count in msg_top_keys.most_common():
        print(f"  {key:<32}  {count}")
    print("\nSenders / roles:")
    for sender, count in sender_counter.most_common():
        print(f"  {sender:<32}  {count}")
    if content_top_keys:
        print("\nKeys dentro de content[] (cuando content es lista):")
        for key, count in content_top_keys.most_common():
            print(f"  {key:<32}  {count}")
        print("\nTipos de content blocks (content[].type):")
        for ctype, count in content_types.most_common():
            print(f"  {ctype:<32}  {count}")
    if attachment_counter:
        print("\nAttachments / files:")
        for k, v in attachment_counter.most_common():
            print(f"  {k:<32}  {v}")
    print("\nModo de texto por mensaje (qué campos están poblados):")
    for mode, count in text_mode_counter.most_common():
        print(f"  {mode:<32}  {count}")
    if text_field_chars:
        print(
            f"\nCampo 'text' (n={len(text_field_chars)}): "
            f"min={min(text_field_chars)}, max={max(text_field_chars):,}, "
            f"mean={mean(text_field_chars):.0f}, median={median(text_field_chars)}"
        )
    if content_field_chars:
        print(
            f"Campo 'content[].text' agregado (n={len(content_field_chars)}): "
            f"min={min(content_field_chars)}, max={max(content_field_chars):,}, "
            f"mean={mean(content_field_chars):.0f}, median={median(content_field_chars)}"
        )
    if char_per_msg:
        print(
            f"Canónico (content si existe, sino text) (n={len(char_per_msg)}): "
            f"min={min(char_per_msg)}, max={max(char_per_msg):,}, "
            f"mean={mean(char_per_msg):.0f}, median={median(char_per_msg)}"
        )
    if chars_per_conv:
        print(
            f"Caracteres por conversación: min={min(chars_per_conv)}, "
            f"max={max(chars_per_conv):,}, mean={mean(chars_per_conv):.0f}, "
            f"median={median(chars_per_conv)}"
        )
    print(f"\nMensajes con parent_uuid (indicador de tree/branches): {has_parent_uuid}")
    print(f"Mensajes con campo 'branch' o 'branches': {has_branches}")
    if created_samples:
        print(f"\nMuestras de timestamps (created_at): {created_samples}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
