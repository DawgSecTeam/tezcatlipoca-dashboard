"""Operator wiki — serves markdown pages, assets, manifest, and search index.

Single source of truth = top-level ``wiki/`` (see ``WIKI_ROOT`` config).
Backend reads files directly; restart-to-update is the contract.

Auth: red / white / black / admin only. Blue team → 403. Reuses the
``check_non_blue_team`` dependency from ``routes/iocs.py`` for consistency.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from fastapi_backend.core.config import config
from fastapi_backend.core.ioc_definition import IOCDefinitionLoader
from fastapi_backend.routes.iocs import check_non_blue_team, get_ioc_loader
from fastapi_backend.routes.iocs import ioc_loader as _module_ioc_loader
from fastapi_backend.utils.auth import Users

router = APIRouter(prefix="/wiki", tags=["wiki"])
logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _wiki_root() -> Path:
    return Path(config.WIKI_ROOT).resolve()


def _resolve_under_root(rel: Path) -> Path:
    """Resolve `rel` under WIKI_ROOT; raise 400 on traversal."""
    root = _wiki_root()
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status_code=400, detail={"code": "wiki_path_traversal"})
    return candidate


def _parse_sidebar(text: str) -> list[dict[str, Any]]:
    """Parse `_Sidebar.md` into a nested tree.

    Recognized line shapes:
      - ``- [Title](slug)``                → leaf
      - ``- **Section**`` or ``- Section`` → section header (children indented)
      - blank lines ignored

    Indentation is two-space steps; deeper indent = child of the most recent
    section at the previous level.
    """
    link_re = re.compile(r"^\s*-\s*\[([^\]]+)\]\(([^)]+)\)\s*$")
    section_re = re.compile(r"^\s*-\s*\*?\*?([^\[\]*]+?)\*?\*?\s*$")

    root: list[dict[str, Any]] = []
    # stack of (indent, children_list)
    stack: list[tuple[int, list[dict[str, Any]]]] = [(-1, root)]

    for raw in text.splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else root

        m = link_re.match(raw)
        if m:
            title, slug = m.group(1).strip(), m.group(2).strip()
            parent.append({"title": title, "slug": slug, "children": []})
            continue

        m = section_re.match(raw)
        if m:
            title = m.group(1).strip()
            node: dict[str, Any] = {"title": title, "slug": None, "children": []}
            parent.append(node)
            stack.append((indent, node["children"]))
            continue

    return root


def _collect_slugs() -> list[str]:
    """List all `.md` files under WIKI_ROOT (recursive), as slugs.

    `Home.md` → `Home`. `c2-generation.md` → `c2-generation`. `_Sidebar.md`
    is excluded from the slug listing (it powers the sidebar tree, not a
    standalone page).
    """
    root = _wiki_root()
    if not root.exists():
        return []
    slugs: list[str] = []
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root)
        if rel.name == "_Sidebar.md":
            continue
        slug = rel.with_suffix("").as_posix()
        slugs.append(slug)
    return slugs


@router.get("/manifest")
async def get_manifest(
    current_user: Users = Depends(check_non_blue_team),
    loader: IOCDefinitionLoader | None = Depends(get_ioc_loader),
) -> dict[str, Any]:
    """Return the list of slugs and the parsed sidebar tree."""
    slugs = _collect_slugs()
    # Include virtual IOC reference pages so the frontend can navigate to them.
    _loader = loader if isinstance(loader, IOCDefinitionLoader) else _module_ioc_loader
    if not _loader.ioc_definitions:
        _loader.load_ioc_definitions()
    virtual = ["iocs"] + [f"iocs/{name}" for name in sorted(_loader.ioc_definitions)]
    slugs = sorted(set(slugs) | set(virtual))

    sidebar_path = _wiki_root() / "_Sidebar.md"
    sidebar_tree: list[dict[str, Any]] = []
    if sidebar_path.is_file():
        try:
            sidebar_tree = _parse_sidebar(sidebar_path.read_text(encoding="utf-8"))
        except Exception as e:  # pragma: no cover - defensive
            logger.error("Failed to parse _Sidebar.md: %s", e)
            sidebar_tree = []

    return {"slugs": slugs, "sidebar": sidebar_tree}


_DIFF_LABEL = {1: "Easy", 2: "Medium", 3: "Hard", 4: "Ultra"}
_DIFF_PTS = {1: 10, 2: 15, 3: 20, 4: 25}
_OS_LABEL = {"windows": "Windows", "linux": "Linux", "firewall": "Firewall (pfSense)"}


def _ioc_index_md(loader: IOCDefinitionLoader) -> str:
    """Generate the IOC reference index page as markdown."""
    iocs = list(loader.ioc_definitions.values())
    total = len(iocs)
    lines = [
        "# IOC Reference",
        "",
        f"Auto-generated from `iocs/definitions/`. **{total} indicators** across 3 platforms.",
        "Detection and remediation guidance is for red-team / admin eyes only.",
        "",
    ]
    for os_key in ("windows", "linux", "firewall"):
        os_iocs = sorted(
            [i for i in iocs if i.os == os_key],
            key=lambda i: (i.difficulty, i.name),
        )
        if not os_iocs:
            continue
        os_label = _OS_LABEL.get(os_key, os_key.capitalize())
        lines += [f"## {os_label}", ""]
        lines += ["| IOC | Difficulty | Category | Points | Deploy Script |", "|-----|------------|----------|--------|---------------|"]
        for ioc in os_iocs:
            diff = _DIFF_LABEL.get(ioc.difficulty, str(ioc.difficulty))
            pts = _DIFF_PTS.get(ioc.difficulty, 0)
            has_deploy = "✓" if ioc.deploy_script else "—"
            lines.append(
                f"| [{ioc.name}](iocs/{ioc.name}) | {diff} | {ioc.category} | {pts} | {has_deploy} |"
            )
        lines.append("")
    return "\n".join(lines)


def _ioc_detail_md(ioc: Any) -> str:
    """Generate a single-IOC wiki page as markdown."""
    from iocs.base import DefaultCredCheck, SliverCheck

    diff = _DIFF_LABEL.get(ioc.difficulty, str(ioc.difficulty))
    pts = _DIFF_PTS.get(ioc.difficulty, 0)
    os_label = _OS_LABEL.get(ioc.os, ioc.os.capitalize())

    check_type = (
        "Sliver session/beacon" if isinstance(ioc.check, SliverCheck)
        else "NXC auth probe (DefaultCredCheck)" if isinstance(ioc.check, DefaultCredCheck)
        else "NXC script"
    )

    lines = [
        f"# {ioc.name}",
        "",
        f"**{ioc.description}**",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| OS | {os_label} |",
        f"| Category | {ioc.category} |",
        f"| Difficulty | {diff} ({pts} pts) |",
        f"| Check method | {check_type} |",
        f"| Deploy script | `{ioc.deploy_script}` |" if ioc.deploy_script else "| Deploy script | — |",
        f"| Remediate script | `{ioc.remediate_script}` |" if ioc.remediate_script else "| Remediate script | — |",
        "",
    ]

    if ioc.discovery and ioc.discovery != "Not specified":
        lines += ["## Discovery Method", "", f"```\n{ioc.discovery}\n```", ""]

    if ioc.detection_steps:
        lines += ["## Detection Steps", ""]
        for i, step in enumerate(ioc.detection_steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    if ioc.remediation_steps:
        lines += ["## Remediation Steps", ""]
        for i, step in enumerate(ioc.remediation_steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    if ioc.artifacts:
        lines += ["## Artifacts", "", "| Path / Name | Type | Notes |", "|-------------|------|-------|"]
        for a in ioc.artifacts:
            if a.dynamic:
                path_col = "*(dynamic)*"
                type_col = "Dynamic"
            else:
                path_col = f"`{a.path}`" if a.path else "—"
                type_col = "Static"
            desc = a.description.replace("|", "\\|") if a.description else ""
            lines.append(f"| {path_col} | {type_col} | {desc} |")
        lines.append("")

    if ioc.persistence:
        p = ioc.persistence
        lines += [
            "## Persistence Mechanism",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Name | `{p.name}` |",
            f"| Tier | {p.tier} |",
            f"| Retrigger interval | {p.retrigger_interval_seconds}s |",
            f"| Immutable flag | {'Yes' if p.immutable else 'No'} |",
            f"| Check script | `{p.check_script}` |",
        ]
        if p.deploy_script:
            lines.append(f"| Deploy script | `{p.deploy_script}` |")
        if p.remediate_script:
            lines.append(f"| Remediate script | `{p.remediate_script}` |")
        lines.append("")

    if ioc.payloads:
        lines += ["## Payloads", ""]
        for p in ioc.payloads:
            lines.append(f"- `iocs/payloads/{p}`")
        lines.append("")

    if ioc.added_credentials:
        lines += ["## Auto-Registered Credentials", ""]
        for c in ioc.added_credentials:
            lines.append(f"- `{c.username}` / `{c.password}` (protocol: `{c.protocol}`)")
        lines.append("")

    lines += [
        "---",
        "",
        "*← [IOC Reference](iocs)*",
        "",
    ]
    return "\n".join(lines)


@router.get("/page/{slug:path}", response_class=PlainTextResponse)
async def get_page(
    slug: str,
    current_user: Users = Depends(check_non_blue_team),
    loader: IOCDefinitionLoader | None = Depends(get_ioc_loader),
) -> str:
    """Return raw markdown text for the requested slug.

    Virtual pages under the ``iocs/`` prefix are generated dynamically from the
    live IOC definition loader — no on-disk ``.md`` file required.
    """
    if not slug or any(part in ("", "..", ".") for part in slug.split("/")):
        raise HTTPException(status_code=400, detail={"code": "wiki_bad_slug"})

    # ── virtual IOC reference pages ──────────────────────────────────────────
    if slug == "iocs" or slug.startswith("iocs/"):
        _loader = loader if isinstance(loader, IOCDefinitionLoader) else _module_ioc_loader
        if not _loader.ioc_definitions:
            _loader.load_ioc_definitions()
        if slug == "iocs":
            return _ioc_index_md(_loader)
        ioc_name = slug[len("iocs/"):]
        ioc = _loader.ioc_definitions.get(ioc_name)
        if ioc is None:
            raise HTTPException(status_code=404, detail={"code": "wiki_page_not_found"})
        return _ioc_detail_md(ioc)
    # ─────────────────────────────────────────────────────────────────────────

    target = _resolve_under_root(Path(f"{slug}.md"))
    if not target.is_file():
        raise HTTPException(status_code=404, detail={"code": "wiki_page_not_found"})
    return target.read_text(encoding="utf-8")


@router.get("/assets/{slug}/{filename}")
async def get_asset(
    slug: str,
    filename: str,
    current_user: Users = Depends(check_non_blue_team),
) -> FileResponse:
    """Serve a static binary asset attached to a wiki page."""
    if not _SLUG_RE.match(slug) or not _SLUG_RE.match(filename):
        raise HTTPException(status_code=400, detail={"code": "wiki_bad_asset_name"})
    # Belt-and-braces: regex permits `.` and `..`, both of which would let the
    # resolved path climb out of the assets dir. Reject directory specials.
    if slug in {".", ".."} or filename in {".", ".."}:
        raise HTTPException(status_code=400, detail={"code": "wiki_bad_asset_name"})

    target = _resolve_under_root(Path("assets") / slug / filename)
    if not target.is_file():
        raise HTTPException(status_code=404, detail={"code": "wiki_asset_not_found"})
    return FileResponse(target)


@router.get("/index.json")
async def get_search_index(
    current_user: Users = Depends(check_non_blue_team),
) -> Any:
    """Return the prebuilt MiniSearch index JSON, or an empty stub.

    Built by ``react/scripts/build-wiki-index.mjs`` at prebuild time. Returns
    an empty index when the file is missing so the frontend can render the
    search UI without a 404.
    """
    target = _wiki_root() / "index.json"
    if not target.is_file():
        return {"documents": [], "index": None}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.error("Wiki index.json invalid: %s", e)
        raise HTTPException(status_code=500, detail={"code": "wiki_index_invalid"}) from e
