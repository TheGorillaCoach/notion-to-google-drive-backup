"""Convert Notion rich text, blocks and properties into GitHub-flavored Markdown.

Pure functions: no API calls. The exporter attaches each block's children under
the key "_children" before rendering, and passes a `Resolver` that knows titles
and file paths of other pages for links/relations/mentions.
"""
from __future__ import annotations

import json
from typing import Any, Protocol


class Resolver(Protocol):
    def title(self, notion_id: str) -> str | None: ...
    def link(self, notion_id: str) -> str | None: ...   # relative path or URL
    def user(self, user_id: str) -> str | None: ...


# ---------------------------------------------------------------------------
# Rich text
# ---------------------------------------------------------------------------
def _wrap(text: str, marker: str) -> str:
    """Apply an inline marker without breaking on leading/trailing spaces."""
    stripped = text.strip()
    if not stripped:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return f"{lead}{marker}{stripped}{marker}{trail}"


def rich_text(segments: list[dict] | None, res: Resolver | None = None) -> str:
    out: list[str] = []
    for seg in segments or []:
        kind = seg.get("type")
        text = seg.get("plain_text", "")
        href = seg.get("href")
        if kind == "equation":
            out.append(f"${seg['equation']['expression']}$")
            continue
        if kind == "mention":
            m = seg["mention"]
            mtype = m.get("type")
            if mtype in ("page", "database") and res:
                target = m[mtype]["id"]
                text = res.title(target) or text
                href = res.link(target) or href
            elif mtype == "user" and res:
                text = "@" + (res.user(m["user"]["id"]) or text.lstrip("@"))
                href = None
            elif mtype == "date":
                text = _date_text(m["date"])
                href = None
        a = seg.get("annotations", {})
        if a.get("code"):
            text = f"`{text}`"
        else:
            if a.get("bold"):
                text = _wrap(text, "**")
            if a.get("italic"):
                text = _wrap(text, "*")
            if a.get("strikethrough"):
                text = _wrap(text, "~~")
        if href and text.strip():
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def plain(segments: list[dict] | None) -> str:
    return "".join(s.get("plain_text", "") for s in segments or [])


def _date_text(d: dict | None) -> str:
    if not d:
        return ""
    start, end = d.get("start") or "", d.get("end")
    return f"{start} → {end}" if end else start


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------
_SKIP = {"table_of_contents", "breadcrumb", "template", "button"}
_MEDIA = {"image": "🖼 Image", "video": "🎞 Video", "audio": "🔊 Audio",
          "file": "📎 File", "pdf": "📄 PDF"}


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.split("\n"))


def render_blocks(blocks: list[dict], res: Resolver) -> str:
    """Render a list of sibling blocks. Lists are kept tight; other blocks spaced."""
    parts: list[str] = []
    prev_type = None
    for b in blocks:
        md = render_block(b, res)
        if md is None:
            continue
        btype = b["type"]
        tight = btype == prev_type and btype in (
            "bulleted_list_item", "numbered_list_item", "to_do")
        parts.append(("\n" if tight else "\n\n") + md if parts else md)
        prev_type = btype
    return "".join(parts)


def _children(b: dict, res: Resolver) -> str:
    return render_blocks(b.get("_children", []), res)


def render_block(b: dict, res: Resolver) -> str | None:
    t = b["type"]
    data: dict[str, Any] = b.get(t, {}) or {}
    text = rich_text(data.get("rich_text"), res)
    kids = _children(b, res)

    if t in _SKIP:
        return None
    if t == "paragraph":
        body = text
        return body + ("\n\n" + _indent(kids, "    ") if kids else "") if body or kids else None
    if t in ("heading_1", "heading_2", "heading_3"):
        md = "#" * int(t[-1]) + " " + text
        return md + ("\n\n" + kids if kids else "")  # toggleable headings
    if t in ("bulleted_list_item", "numbered_list_item", "to_do"):
        marker = {"bulleted_list_item": "- ", "numbered_list_item": "1. "}.get(t)
        if t == "to_do":
            marker = "- [x] " if data.get("checked") else "- [ ] "
        pad = " " * (3 if t == "numbered_list_item" else 2)
        return marker + text + ("\n" + _indent(kids, pad) if kids else "")
    if t == "toggle":
        return "- ▸ " + text + ("\n" + _indent(kids, "  ") if kids else "")
    if t == "quote":
        body = text + ("\n\n" + kids if kids else "")
        return _indent(body, "> ").replace("\n\n", "\n>\n")
    if t == "callout":
        icon = _icon(data.get("icon"))
        body = (icon + " " if icon else "") + text + ("\n\n" + kids if kids else "")
        return _indent(body, "> ").replace("\n\n", "\n>\n")
    if t == "code":
        lang = data.get("language", "")
        lang = "" if lang == "plain text" else lang.replace(" ", "")
        code = plain(data.get("rich_text"))
        cap = rich_text(data.get("caption"), res)
        return f"```{lang}\n{code}\n```" + (f"\n*{cap}*" if cap else "")
    if t == "equation":
        return f"$$\n{data.get('expression', '')}\n$$"
    if t == "divider":
        return "---"
    if t in _MEDIA:
        return _media(t, data, res)
    if t in ("bookmark", "embed", "link_preview"):
        url = data.get("url", "")
        cap = rich_text(data.get("caption"), res)
        return f"🔗 [{cap or url}]({url})" if url else None
    if t == "child_page":
        target = b["id"]
        title = data.get("title") or res.title(target) or "Untitled"
        link = res.link(target)
        return f"📄 [{title}]({link})" if link else f"📄 {title}"
    if t == "child_database":
        target = b["id"]
        title = data.get("title") or res.title(target) or "Untitled database"
        link = res.link(target)
        return f"🗂 [{title}]({link})" if link else f"🗂 {title}"
    if t == "link_to_page":
        target = data.get(data.get("type", ""), "")
        title = res.title(target) or "Linked page"
        link = res.link(target)
        return f"↗ [{title}]({link})" if link else f"↗ {title}"
    if t == "table":
        return _table(b, res)
    if t == "transcription":
        return _meeting(b, data, res)
    if t in ("column_list", "column", "synced_block"):
        return kids or None
    # Unknown / newer block types (e.g. meeting notes): keep any text + children.
    if text or kids:
        return "\n\n".join(p for p in (text, kids) if p)
    return f"<!-- unsupported Notion block: {t} -->"


def _icon(icon: dict | None) -> str:
    if not icon:
        return ""
    return icon.get("emoji", "") if icon.get("type") == "emoji" else ""


def _media(t: str, data: dict, res: Resolver) -> str:
    label = _MEDIA[t]
    cap = rich_text(data.get("caption"), res)
    name = data.get("name") or ""
    desc = cap or name
    if data.get("type") == "external":
        url = data["external"]["url"]
        if t == "image":
            return f"![{desc or 'image'}]({url})"
        return f"{label}: [{desc or url}]({url})"
    # Notion-hosted file URLs expire after ~1 hour, so don't store them.
    return f"{label}{': ' + desc if desc else ''} *(stored in Notion)*"


def _meeting(b: dict, data: dict, res: Resolver) -> str:
    title = rich_text(data.get("title"), res) or "Meeting notes"
    out = [f"## 🎙 {title}"]
    event = data.get("calendar_event") or {}
    if event.get("start_time"):
        when = event["start_time"] + (f" → {event['end_time']}" if event.get("end_time") else "")
        people = [res.user(a) or a for a in event.get("attendees", [])]
        out.append(f"*{when}*" + (f" · Attendees: {', '.join(people)}" if people else ""))
    any_content = False
    for label, blocks in b.get("_sections", []):
        body = render_blocks(blocks, res).strip()
        if body:
            out.append(f"### {label}\n\n{body}")
            any_content = True
    if not any_content:
        out.append("*(No notes, summary or transcript available to the API.)*")
    return "\n\n".join(out)


def _table(b: dict, res: Resolver) -> str:
    rows = [r for r in b.get("_children", []) if r["type"] == "table_row"]
    if not rows:
        return ""
    cells = [[rich_text(c, res).replace("|", "\\|").replace("\n", "<br>")
              for c in r["table_row"]["cells"]] for r in rows]
    width = max(len(r) for r in cells)
    cells = [r + [""] * (width - len(r)) for r in cells]
    if b["table"].get("has_column_header"):
        header, body = cells[0], cells[1:]
    else:
        header, body = [""] * width, cells
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Properties (database row fields) → plain values for YAML front matter
# ---------------------------------------------------------------------------
def property_value(prop: dict, res: Resolver) -> Any:
    t = prop.get("type")
    v = prop.get(t)
    if v is None:
        return None
    if t in ("title", "rich_text"):
        return plain(v) or None
    if t in ("number", "checkbox", "url", "email", "phone_number",
             "created_time", "last_edited_time"):
        return v
    if t in ("select", "status"):
        return v.get("name")
    if t == "multi_select":
        return [o["name"] for o in v] or None
    if t == "date":
        return _date_text(v) or None
    if t == "people":
        return [res.user(p["id"]) or p.get("name") or p["id"] for p in v] or None
    if t in ("created_by", "last_edited_by"):
        return res.user(v["id"]) or v.get("name") or v["id"]
    if t == "relation":
        return [res.title(r["id"]) or r["id"] for r in v] or None
    if t == "files":
        return [f.get("name") for f in v] or None
    if t == "formula":
        return v.get(v.get("type"))
    if t == "rollup":
        rt = v.get("type")
        if rt == "array":
            vals = [property_value(x, res) for x in v.get("array", [])]
            flat: list = []
            for x in vals:
                flat.extend(x if isinstance(x, list) else [x])
            return [x for x in flat if x is not None] or None
        if rt == "date":
            return _date_text(v.get("date")) or None
        return v.get(rt)
    if t == "unique_id":
        prefix = v.get("prefix")
        return f"{prefix}-{v['number']}" if prefix else v.get("number")
    if t == "verification":
        return v.get("state")
    return None  # button, etc.


def page_title(page: dict) -> str:
    if isinstance(page.get("title"), list):  # data source / database
        return plain(page["title"]).strip()
    for p in page.get("properties", {}).values():
        if p.get("type") == "title":
            return plain(p["title"]).strip()
    return ""


# ---------------------------------------------------------------------------
# YAML front matter (JSON-quoted scalars are valid YAML and always safe)
# ---------------------------------------------------------------------------
def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return json.dumps(str(v), ensure_ascii=False)


def front_matter(meta: dict[str, Any], properties: dict[str, Any] | None = None) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if v is not None:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    if properties:
        lines.append("properties:")
        for k, v in properties.items():
            key = json.dumps(k, ensure_ascii=False)
            if isinstance(v, list):
                lines.append(f"  {key}: [{', '.join(_yaml_scalar(x) for x in v)}]")
            else:
                lines.append(f"  {key}: {_yaml_scalar(v)}")
    lines.append("---")
    return "\n".join(lines)


def table_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "✓" if v else ""
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v)
    s = str(v).replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= 120 else s[:117] + "…"

