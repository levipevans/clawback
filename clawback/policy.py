"""Path policy and mount planning.

Two layers, deliberately:

1. The Docker mount table is the *enforcement*. Only /work and the granted
   roots exist inside the container. Writable grants are mounted at /rw/<name>
   (rw); read-only grants at /ro/<name> (ro).
2. This module is defense in depth: it normalizes tool paths, rejects
   traversal/backslashes, and refuses writes outside a writable root before a
   command is ever sent to Docker.

Hiding a folder is also enforced at the mount layer: an empty directory (or
file) is mounted over the hidden path, so the real contents are not merely
"not requested" -- they are absent from the container's view.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .paths import is_sensitive_name, normalize, standard_read_roots, workspace_dir
from .state import StateStore

WORK = "/work"
RO = "/ro"
RW = "/rw"


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class RootGrant:
    slug: str
    host: Path
    container: str
    writable: bool


@dataclass(frozen=True)
class Mount:
    host: Path
    container: str
    read_only: bool


@dataclass(frozen=True)
class Resolved:
    path: str
    writable: bool
    root: str


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._-")
    return cleaned or "root"


def _overlaps(a: Path, b: Path) -> bool:
    a = a.resolve()
    b = b.resolve()
    return a == b or a in b.parents or b in a.parents


def plan_roots(
    sandbox: Path,
    include_standard: bool,
    extra_read_roots: list[str],
    extra_write_roots: list[str],
    hidden_roots: list[str],
) -> tuple[list[RootGrant], list[Path]]:
    candidates: list[tuple[str, Path, bool]] = []
    if include_standard:
        candidates.extend((name, path, False) for name, path in standard_read_roots().items())
    for raw in extra_read_roots:
        path = normalize(raw)
        candidates.append((path.name or "root", path, False))
    for raw in extra_write_roots:
        path = normalize(raw)
        candidates.append((path.name or "root", path, True))

    hidden = [normalize(p) for p in hidden_roots]

    grants: dict[Path, RootGrant] = {}
    order: list[Path] = []
    used_slugs: set[str] = set()

    for label, path, writable in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.exists() or not resolved.is_dir():
            continue
        if is_sensitive_name(resolved):
            continue
        if _overlaps(resolved, sandbox):
            # Do not double-mount the workspace or expose its parent.
            continue
        # A root that is itself hidden, or inside a hidden folder, is dropped.
        if any(resolved == h or h in resolved.parents for h in hidden):
            continue

        existing = grants.get(resolved)
        if existing is not None:
            # If the same folder is both read and write, the write grant wins.
            if writable and not existing.writable:
                grants[resolved] = RootGrant(existing.slug, resolved, f"{RW}/{existing.slug}", True)
            continue

        base = _slug(label)
        slug = base
        counter = 2
        while slug in used_slugs:
            slug = f"{base}-{counter}"
            counter += 1
        used_slugs.add(slug)
        container = f"{RW if writable else RO}/{slug}"
        grants[resolved] = RootGrant(slug=slug, host=resolved, container=container, writable=writable)
        order.append(resolved)

    roots = [grants[path] for path in order]
    roots.sort(key=lambda r: r.slug.lower())
    return roots, hidden


class Policy:
    def __init__(
        self,
        sandbox: Path,
        roots: list[RootGrant],
        hidden: list[Path],
        empty_dir: Path,
        empty_file: Path,
    ):
        self.sandbox = sandbox.resolve()
        self.roots = roots
        self.hidden = hidden
        self.empty_dir = empty_dir
        self.empty_file = empty_file
        self._hidden_targets = self._plan_hidden_overlays()

    def _plan_hidden_overlays(self) -> list[tuple[Path, str]]:
        overlays: list[tuple[Path, str]] = []
        seen: set[str] = set()
        for hidden in self.hidden:
            for root in self.roots:
                if hidden == root.host or hidden in root.host.parents:
                    continue
                if root.host not in hidden.parents:
                    continue
                try:
                    rel = hidden.relative_to(root.host)
                except ValueError:
                    continue
                target = f"{root.container}/{rel.as_posix()}"
                if target in seen:
                    continue
                seen.add(target)
                source = self.empty_file if hidden.is_file() else self.empty_dir
                overlays.append((source, target))
        return overlays

    # -- mount planning ---------------------------------------------------
    def mounts(self) -> list[Mount]:
        mounts = [Mount(self.sandbox, WORK, False)]
        mounts.extend(Mount(r.host, r.container, not r.writable) for r in self.roots)
        mounts.extend(Mount(src, target, True) for src, target in self._hidden_targets)
        return mounts

    def roots_manifest(self) -> str:
        lines = [f"{WORK}  (read-write)"]
        for root in self.roots:
            lines.append(f"{root.container}  ({'read-write' if root.writable else 'read-only'})")
        if not self.roots:
            lines.append("(no extra folders are mounted)")
        return "\n".join(lines)

    def describe(self) -> list[tuple[str, str, str]]:
        rows = [(WORK, str(self.sandbox), "rw")]
        rows.extend((r.container, str(r.host), "rw" if r.writable else "ro") for r in self.roots)
        rows.extend((target, "hidden (empty mount)", "ro") for _, target in self._hidden_targets)
        return rows

    def writable_roots(self) -> list[RootGrant]:
        return [r for r in self.roots if r.writable]

    # -- tool path checks -------------------------------------------------
    def resolve(self, raw: str, *, for_write: bool = False) -> Resolved:
        if not isinstance(raw, str) or not raw:
            raise PolicyError("path must be a non-empty string")
        if "\x00" in raw:
            raise PolicyError("path contains a NUL byte")
        if "\\" in raw:
            raise PolicyError("use POSIX paths with '/', not backslashes")
        if not raw.startswith("/") or raw.startswith("//"):
            raise PolicyError("path must be absolute inside the sandbox (e.g. /work/notes.txt)")

        normalized = posixpath.normpath(raw)
        if normalized in (".", "/") or normalized.startswith(".."):
            raise PolicyError("path escapes the sandbox")

        writable = False
        root_name = ""
        if normalized == WORK or normalized.startswith(WORK + "/"):
            writable = True
            root_name = "work"
        else:
            for root in self.roots:
                if normalized == root.container or normalized.startswith(root.container + "/"):
                    writable = root.writable
                    root_name = root.slug
                    break
            else:
                raise PolicyError(
                    f"{normalized!r} is outside the sandbox. Visible roots: "
                    + ", ".join([WORK] + [r.container for r in self.roots])
                )

        for _, target in self._hidden_targets:
            if normalized == target or normalized.startswith(target + "/"):
                raise PolicyError("that folder is hidden by the owner")

        if for_write and not writable:
            raise PolicyError(
                "read-only root: writes are only allowed under /work or a writable grant (/rw/...)"
            )

        return Resolved(path=normalized, writable=writable, root=root_name)


def build_policy(config: AppConfig, state: StateStore, profile: Path) -> Policy:
    sandbox = workspace_dir(profile, config.sandbox_dir)
    extra_read = list(config.extra_read_roots) + state.extra_roots()
    extra_write = list(config.extra_write_roots) + state.extra_write_roots()
    hidden = list(config.hidden_roots) + state.hidden_roots()
    roots, hidden_paths = plan_roots(
        sandbox=sandbox,
        include_standard=config.include_standard_read_roots,
        extra_read_roots=extra_read,
        extra_write_roots=extra_write,
        hidden_roots=hidden,
    )
    runtime = profile / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    empty_dir = runtime / "empty_dir"
    empty_dir.mkdir(parents=True, exist_ok=True)
    empty_file = runtime / "empty_file"
    if not empty_file.exists():
        empty_file.write_bytes(b"")
    return Policy(
        sandbox=sandbox,
        roots=roots,
        hidden=hidden_paths,
        empty_dir=empty_dir,
        empty_file=empty_file,
    )
