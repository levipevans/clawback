"""Structured file operations executed *inside* the sandbox container.

The container's mount table is the security boundary. These checks exist only
to fail early and to keep the tool surface tidy; they are not the sandbox.
Do not add host paths, network calls, or shell execution here.
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import shutil
import sys
import time

READ_ROOTS = ("/work", "/ro", "/rw")
WRITE_ROOTS = ("/work", "/rw")


class OpError(ValueError):
    pass


def _normalize(raw: object) -> str:
    if not isinstance(raw, str) or not raw:
        raise OpError("path is required")
    if "\x00" in raw:
        raise OpError("NUL byte in path")
    if not raw.startswith("/") or raw.startswith("//"):
        raise OpError("path must be absolute")
    path = posixpath.normpath(raw)
    if path in (".", "/") or path.startswith(".."):
        raise OpError("path escapes the allowed roots")
    return path


def _inside(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _check_read(raw: object) -> str:
    path = _normalize(raw)
    if not _inside(path, READ_ROOTS):
        raise OpError("read denied: outside /work and /ro")
    real = os.path.realpath(path)
    if not _inside(real, READ_ROOTS):
        raise OpError("read denied: symlink escapes the allowed roots")
    return path


def _check_write(raw: object) -> str:
    path = _normalize(raw)
    if not _inside(path, WRITE_ROOTS):
        raise OpError("write denied: only /work and writable grants are writable")
    real = os.path.realpath(path)
    if not _inside(real, WRITE_ROOTS):
        raise OpError("write denied: symlink escapes the writable roots")
    return path


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _entry(path: str, name: str) -> dict:
    full = os.path.join(path, name) if path != "/" else "/" + name
    try:
        st = os.lstat(full)
    except OSError as exc:
        return {"name": name, "path": full, "type": "unknown", "error": str(exc)}
    if os.path.islink(full):
        kind = "link"
    elif os.path.isdir(full):
        kind = "dir"
    else:
        kind = "file"
    item = {
        "name": name,
        "path": full,
        "type": kind,
        "size": st.st_size,
        "mtime": _iso(st.st_mtime),
    }
    if kind == "link":
        try:
            item["target"] = os.readlink(full)
        except OSError:
            pass
    return item


def op_list(args: dict) -> dict:
    path = _check_read(args.get("path", "/work"))
    depth = max(1, min(int(args.get("depth", 1)), 3))
    limit = max(1, min(int(args.get("max_entries", 500)), 5000))
    entries: list[dict] = []
    stack: list[tuple[str, int]] = [(path, 0)]
    while stack and len(entries) < limit:
        current, level = stack.pop()
        try:
            with os.scandir(current) as iterator:
                names = sorted(e.name for e in iterator)
        except OSError as exc:
            entries.append({"path": current, "type": "error", "error": str(exc)})
            continue
        for name in names:
            if len(entries) >= limit:
                break
            item = _entry(current, name)
            entries.append(item)
            if (
                item["type"] == "dir"
                and level + 1 < depth
                and not name.startswith(".")
            ):
                stack.append((item["path"], level + 1))
    return {"ok": True, "path": path, "entries": entries, "truncated": len(entries) >= limit}


def op_read(args: dict) -> dict:
    path = _check_read(args.get("path"))
    limit = max(1, min(int(args.get("max_bytes", 120_000)), 1_000_000))
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    truncated = len(data) > limit
    data = data[:limit]
    binary = b"\x00" in data
    result = {
        "ok": True,
        "path": path,
        "size": os.path.getsize(path),
        "truncated": truncated,
        "binary": binary,
    }
    if binary:
        result["note"] = "binary file; content not returned"
    else:
        result["text"] = data.decode("utf-8", errors="replace")
    return result


def op_stat(args: dict) -> dict:
    path = _check_read(args.get("path"))
    st = os.lstat(path)
    result = {
        "ok": True,
        "path": path,
        "type": "link" if os.path.islink(path) else ("dir" if os.path.isdir(path) else "file"),
        "size": st.st_size,
        "mtime": _iso(st.st_mtime),
        "mode": oct(st.st_mode & 0o777),
    }
    if os.path.islink(path):
        result["target"] = os.readlink(path)
    return result


def op_search(args: dict) -> dict:
    root = _check_read(args.get("path", "/work"))
    glob = str(args.get("glob", "**/*"))
    pattern = args.get("pattern")
    regex = re.compile(str(pattern)) if pattern else None
    max_results = max(1, min(int(args.get("max_results", 50)), 200))
    max_files = max(1, min(int(args.get("max_files", 2000)), 10_000))
    max_file_bytes = max(1024, min(int(args.get("max_file_bytes", 1_000_000)), 5_000_000))

    matches: list[dict] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for filename in sorted(filenames):
            if len(matches) >= max_results or scanned >= max_files:
                break
            scanned += 1
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(filename, glob):
                continue
            if regex is None:
                matches.append({"path": full, "line": None, "text": None})
                continue
            try:
                if os.path.getsize(full) > max_file_bytes:
                    continue
                with open(full, "r", encoding="utf-8", errors="ignore") as handle:
                    for lineno, line in enumerate(handle, 1):
                        if regex.search(line):
                            matches.append({"path": full, "line": lineno, "text": line.rstrip()[:400]})
                            if len(matches) >= max_results:
                                break
            except OSError:
                continue
        if len(matches) >= max_results or scanned >= max_files:
            break
    return {
        "ok": True,
        "root": root,
        "matches": matches,
        "truncated": len(matches) >= max_results or scanned >= max_files,
    }


def op_write(args: dict) -> dict:
    path = _check_write(args.get("path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise OpError("content must be a string")
    if len(content.encode("utf-8")) > 4_000_000:
        raise OpError("content too large (4 MB limit)")
    if args.get("create_parents", True):
        os.makedirs(os.path.dirname(path) or "/work", exist_ok=True)
    tmp = f"{path}.clawback-tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    os.replace(tmp, path)
    return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}


def op_append(args: dict) -> dict:
    path = _check_write(args.get("path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise OpError("content must be a string")
    if len(content.encode("utf-8")) > 4_000_000:
        raise OpError("content too large (4 MB limit)")
    if args.get("create_parents", True):
        os.makedirs(os.path.dirname(path) or "/work", exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}


def op_mkdir(args: dict) -> dict:
    path = _check_write(args.get("path"))
    os.makedirs(path, exist_ok=True)
    return {"ok": True, "path": path}


def op_move(args: dict) -> dict:
    src = _check_write(args.get("src"))
    dst = _check_write(args.get("dst"))
    if os.path.exists(dst) and not args.get("overwrite", False):
        raise OpError("destination exists; pass overwrite=true to replace")
    os.makedirs(os.path.dirname(dst) or "/work", exist_ok=True)
    if os.path.exists(dst) and args.get("overwrite"):
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst)
        else:
            os.unlink(dst)
    shutil.move(src, dst)
    return {"ok": True, "src": src, "dst": dst}


def op_copy(args: dict) -> dict:
    src = _check_read(args.get("src"))
    dst = _check_write(args.get("dst"))
    if os.path.exists(dst) and not args.get("overwrite", False):
        raise OpError("destination exists; pass overwrite=true to replace")
    os.makedirs(os.path.dirname(dst) or "/work", exist_ok=True)
    if os.path.isdir(src) and not os.path.islink(src):
        if os.path.exists(dst) and args.get("overwrite"):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    return {"ok": True, "src": src, "dst": dst}


def _trash_dir(path: str) -> str:
    if path == "/work" or path.startswith("/work/"):
        return "/work/.trash"
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "rw" and parts[2]:
        return f"/rw/{parts[2]}/.clawback-trash"
    raise OpError("cannot determine a trash folder for that path")


def op_delete(args: dict) -> dict:
    path = _check_write(args.get("path"))
    if path in ("/work", "/work/.trash"):
        raise OpError("refusing to delete a root")
    if path.startswith("/rw/") and path.count("/") == 2:
        raise OpError("refusing to delete a root")
    if path.endswith("/.clawback-trash"):
        raise OpError("refusing to delete the trash folder itself")
    trash = _trash_dir(path)
    os.makedirs(trash, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = posixpath.basename(path.rstrip("/")) or "item"
    target = posixpath.join(trash, f"{stamp}-{name}")
    counter = 1
    while os.path.exists(target):
        target = posixpath.join(trash, f"{stamp}-{name}-{counter}")
        counter += 1
    shutil.move(path, target)
    return {"ok": True, "path": path, "trashed_to": target}


OPS = {
    "list": op_list,
    "read": op_read,
    "stat": op_stat,
    "search": op_search,
    "write": op_write,
    "append": op_append,
    "mkdir": op_mkdir,
    "move": op_move,
    "copy": op_copy,
    "delete": op_delete,
}


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": f"invalid json: {exc}"}))
        return 0
    op = payload.pop("op", None)
    handler = OPS.get(op)
    if handler is None:
        sys.stdout.write(json.dumps({"ok": False, "error": f"unknown op: {op!r}"}))
        return 0
    try:
        result = handler(payload)
    except (OpError, OSError) as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:  # defensive: never leak a traceback as stdout
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
