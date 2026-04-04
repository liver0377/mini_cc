from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_MEMORY_DIR_NAME = "memory"
_INDEX_FILE = "MEMORY.md"
_MAX_INDEX_LINES = 200
_BASE_DIR = Path.home() / ".mini_cc" / "projects"

_VALID_TYPES = {"user", "feedback", "project", "reference"}


@dataclass(frozen=True)
class MemoryMeta:
    name: str
    type: str
    description: str


@dataclass
class MemoryItem:
    name: str
    type: str
    content: str
    description: str = ""


def project_id(cwd: Path) -> str:
    return hashlib.sha256(str(cwd).encode()).hexdigest()[:12]


def get_memory_dir(cwd: Path) -> Path:
    return _BASE_DIR / project_id(cwd) / _MEMORY_DIR_NAME


def _sanitize_filename(name: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = sanitized.strip("_")
    return sanitized.lower() or "unnamed"


def list_memories(cwd: Path) -> list[MemoryMeta]:
    memory_dir = get_memory_dir(cwd)
    if not memory_dir.is_dir():
        return []
    result: list[MemoryMeta] = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == _INDEX_FILE:
            continue
        meta = _parse_frontmatter(path)
        if meta is not None:
            result.append(meta)
    return result


def save_memory(cwd: Path, name: str, type: str, content: str, description: str = "") -> Path:
    if type not in _VALID_TYPES:
        raise ValueError(f"Invalid memory type: {type!r}. Must be one of {_VALID_TYPES}")
    memory_dir = get_memory_dir(cwd)
    memory_dir.mkdir(parents=True, exist_ok=True)

    filename = _sanitize_filename(name) + ".md"
    filepath = memory_dir / filename

    frontmatter = f"---\nname: {name}\ntype: {type}\n"
    if description:
        frontmatter += f"description: {description}\n"
    frontmatter += "---\n"

    filepath.write_text(frontmatter + content, encoding="utf-8")

    _rebuild_index(cwd)
    return filepath


def load_memory_index(cwd: Path) -> str:
    index_path = get_memory_dir(cwd) / _INDEX_FILE
    if not index_path.is_file():
        return ""
    return index_path.read_text(encoding="utf-8")


def _parse_frontmatter(path: Path) -> MemoryMeta | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    frontmatter = text[3:end].strip()
    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    name = fields.get("name", path.stem)
    mem_type = fields.get("type", "project")
    desc = fields.get("description", "")
    return MemoryMeta(name=name, type=mem_type, description=desc)


def _rebuild_index(cwd: Path) -> None:
    memories = list_memories(cwd)
    lines: list[str] = []
    for mem in memories:
        display = mem.description or mem.name
        lines.append(f"- [{mem.name}]({_sanitize_filename(mem.name)}.md) — {display}")

    if len(lines) > _MAX_INDEX_LINES:
        lines = lines[:_MAX_INDEX_LINES]

    memory_dir = get_memory_dir(cwd)
    index_path = memory_dir / _INDEX_FILE
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
