#!/usr/bin/env python3
"""Workspace temp-file self-check and safe cleanup.

Two-phase design (deletion never happens implicitly):

  scan  -- read-only. Finds candidate temp files, prints a report and a
           short-lived confirmation token derived from the exact candidate set.
  clean -- requires --token from the matching scan. Re-scans, verifies the
           token, then moves ONLY those files to the OS recycle bin.

Safety invariants enforced in code:
  * Operates only inside an explicitly supplied workspace root.
  * Refuses home / Desktop / Documents / Downloads / drive-root as the root.
  * Never follows symlinks and never leaves the root via path traversal.
  * Skips a protected-directory denylist (.git, .workbuddy, node_modules, ...).
  * Recycle bin only -- never permanent deletion, on any platform.
  * Token mismatch -> abort without touching anything.

Usage:
  python clean_temp.py scan  [--root PATH] [--json]
  python clean_temp.py clean --token TOKEN [--root PATH] [--json]

Exit codes:
  0 success / scan complete
  1 usage or validation error
  2 token mismatch or stale scan (nothing deleted)
  3 recycle-bin operation failed for at least one file
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Detection policy (conservative by design -- false positives are reported to
# the human for review, never deleted automatically).
# --------------------------------------------------------------------------- #

# Directories never entered. .workbuddy holds project data and is off-limits.
PROTECTED_DIR_NAMES = {
    ".git", ".svn", ".hg", ".bzr",
    ".workbuddy", ".codebuddy",
    "node_modules", ".venv", "venv", "env", ".env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".gradle", ".idea", ".vscode",
    "dist", "build", "target",
}

# One-off helper script extensions.
SCRIPT_EXTENSIONS = {
    ".py", ".sh", ".bash", ".js", ".jsx", ".ts", ".tsx",
    ".ps1", ".bat", ".cmd", ".rb", ".pl", ".php",
}

# A script must carry one of these markers in its stem to be flagged.
ONEOFF_STEM_MARKERS = (
    "tmp", "temp", "scratch", "oneoff", "one_off",
    "throwaway", "probe", "quicktest", "quick_test",
)

# Filename patterns that mark a generic temp/backup file.
TEMP_SUFFIXES = (
    ".tmp", ".temp", ".bak", ".backup", ".swp", ".swo",
    ".orig", ".rej", ".part", ".partial", ".crdownload",
)
TEMP_STEM_PREFIXES = ("tmp_", "temp_", "_tmp", "_temp", "tmp-", "temp-")
TEMP_STEM_EXACT = {"tmp", "temp", "scratch"}

# Maximum depth / file size are intentionally not limited: a temp script can be
# anywhere in the workspace and still be relevant.

MAX_REPORT_ITEMS = 500  # prevent overwhelming output; still returns full count


# --------------------------------------------------------------------------- #
# Root validation
# --------------------------------------------------------------------------- #

def _norm(value: str) -> str:
    return os.path.normcase(os.path.abspath(value).rstrip(os.sep)).lower()


def validate_root(raw: str) -> Path:
    """Resolve and validate the workspace root; raise ValueError on refusal."""
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"目标不是有效目录: {root}")

    home = Path.home().resolve()
    forbidden = {
        home,
        home / "Desktop", home / "Downloads", home / "Documents",
        home / "Pictures", home / "Music", home / "Videos", home / "桌面",
        home / "下载", home / "文档",
    }
    forbidden_norm = {_norm(str(p)) for p in forbidden}
    root_norm = _norm(str(root))

    if root_norm in forbidden_norm:
        raise ValueError(
            f"拒绝在个人顶层目录执行清理: {root}。请指定一个具体项目工作区子目录。"
        )
    if root == Path(root.anchor):
        raise ValueError("拒绝在磁盘根目录执行清理。")
    return root


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def is_temp_named(name: str) -> bool:
    low = name.lower()
    stem = Path(name).stem.lower()
    if low.startswith("~") or low.endswith("~"):
        return True
    if low.endswith(TEMP_SUFFIXES):
        return True
    if stem in TEMP_STEM_EXACT:
        return True
    if stem.startswith(TEMP_STEM_PREFIXES):
        return True
    # e.g. "notes (conflicted 2026-01-01).txt"
    if re.search(r"\(conflicted copy|\(conflicted \d", low):
        return True
    return False


def is_oneoff_script(name: str) -> bool:
    path = Path(name)
    if path.suffix.lower() not in SCRIPT_EXTENSIONS:
        return False
    stem = path.stem.lower()
    return any(marker in stem for marker in ONEOFF_STEM_MARKERS)


def _classify(name: str) -> list[str]:
    rules: list[str] = []
    if is_temp_named(name):
        rules.append("临时命名文件")
    if is_oneoff_script(name):
        rules.append("一次性辅助脚本")
    return rules


def scan_candidates(root: Path) -> list[dict[str, Any]]:
    """Walk the workspace read-only and return flagged file records."""
    found: list[dict[str, Any]] = []
    root_resolved = root.resolve()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Prune protected and symlinked directories in-place.
        dirnames[:] = [
            d for d in dirnames
            if d not in PROTECTED_DIR_NAMES
            and not os.path.islink(os.path.join(dirpath, d))
        ]

        for filename in filenames:
            full = Path(dirpath) / filename
            if full.is_symlink() or not full.is_file():
                continue

            rules = _classify(filename)
            if not rules:
                continue

            try:
                resolved = full.resolve()
                rel = resolved.relative_to(root_resolved)
            except (ValueError, OSError):
                # Escaped the root or unresolvable -- skip for safety.
                continue

            try:
                stat = full.stat()
            except OSError:
                continue

            found.append({
                "path": str(full),
                "relative": rel.as_posix(),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "rules": rules,
            })

    found.sort(key=lambda item: item["relative"])
    return found


def compute_token(root: Path, candidates: list[dict[str, Any]]) -> str:
    """Fingerprint the exact candidate set so a stale token cannot delete new files."""
    payload = json.dumps(
        {
            "root": str(root.resolve()),
            "files": [
                [c["relative"], c["size"], round(c["mtime"], 3)]
                for c in candidates
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------- #
# Recycle-bin backends -- permanent deletion is never used as a fallback.
# --------------------------------------------------------------------------- #

def _send_to_recycle_bin(paths: list[str]) -> tuple[int, str]:
    """Move files to the OS recycle bin. Returns (succeeded_count, error_message)."""
    if not paths:
        return 0, ""

    if sys.platform == "win32":
        return _recycle_windows(paths)
    if sys.platform == "darwin":
        return _recycle_macos(paths)
    return _recycle_linux(paths)


def _recycle_windows(paths: list[str]) -> tuple[int, str]:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_NOERRORUI = 0x0400
    FOF_SILENT = 0x0004

    # SHFileOperationW expects a double-NUL-terminated multi-string.
    file_list = "\0".join(paths) + "\0"
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = file_list
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT

    try:
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as exc:  # pragma: no cover - defensive
        return 0, f"SHFileOperationW 调用失败: {exc}"

    if result != 0 or op.fAnyOperationsAborted:
        return 0, f"SHFileOperationW 返回码 {result}, aborted={bool(op.fAnyOperationsAborted)}"

    remaining = [p for p in paths if os.path.exists(p)]
    return len(paths) - len(remaining), ""


def _recycle_macos(paths: list[str]) -> tuple[int, str]:
    ok = 0
    errors: list[str] = []
    for path in paths:
        script = f'tell application "Finder" to delete POSIX file "{path}"'
        proc = __import__("subprocess").run(
            ["osascript", "-e", script], capture_output=True, text=True
        )
        if proc.returncode == 0:
            ok += 1
        else:
            errors.append(f"{path}: {proc.stderr.strip()}")
    return ok, "; ".join(errors)


def _recycle_linux(paths: list[str]) -> tuple[int, str]:
    import subprocess

    for tool in ("gio", "trash-put"):
        if subprocess.run(["which", tool], capture_output=True).returncode == 0:
            ok = 0
            errors: list[str] = []
            for path in paths:
                proc = subprocess.run([tool, "trash", path] if tool == "gio" else [tool, path],
                                      capture_output=True, text=True)
                if proc.returncode == 0:
                    ok += 1
                else:
                    errors.append(f"{path}: {proc.stderr.strip()}")
            return ok, "; ".join(errors)
    return 0, "未找到可用的回收站工具 (gio / trash-put)，已中止，未删除任何文件。"


# --------------------------------------------------------------------------- #
# Exclusion support (so a user can keep specific files after the interactive
# confirmation without touching the filesystem by hand).
# --------------------------------------------------------------------------- #

def parse_excludes(raw: str | None) -> list[str]:
    """Parse a comma-separated list of relative paths or bare filenames."""
    if not raw:
        return []
    return [
        part.strip().replace("\\", "/").lstrip("./")
        for part in raw.split(",")
        if part.strip()
    ]


def apply_excludes(
    candidates: list[dict[str, Any]], excludes: list[str]
) -> list[dict[str, Any]]:
    """Drop candidates explicitly excluded by the user.

    Matching supports full relative paths and bare filenames. The token is
    computed AFTER this filter, so scan and clean must receive identical
    --exclude values.
    """
    if not excludes:
        return candidates
    excluded = set(excludes)
    return [
        c for c in candidates
        if c["relative"] not in excluded
        and Path(c["relative"]).name not in excluded
    ]


# --------------------------------------------------------------------------- #
# Formatting / CLI
# --------------------------------------------------------------------------- #

def _human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _human_time(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def format_report(root: Path, candidates: list[dict[str, Any]], token: str) -> str:
    lines = [
        f"工作区: {root}",
        f"自检结果: 发现 {len(candidates)} 个候选临时文件（仅扫描，未做任何修改）",
        "",
    ]
    if not candidates:
        lines.append("无需清理：未发现符合判定标准的文件。")
        lines.append(f"确认令牌 (CONFIRM_TOKEN): {token}")
        return "\n".join(lines)

    for idx, item in enumerate(candidates[:MAX_REPORT_ITEMS], start=1):
        tags = " / ".join(item["rules"])
        lines.append(
            f"{idx:>3}. [{tags}] {item['relative']}  "
            f"({_human_size(item['size'])}, 修改于 {_human_time(item['mtime'])})"
        )
    if len(candidates) > MAX_REPORT_ITEMS:
        lines.append(f"... 另有 {len(candidates) - MAX_REPORT_ITEMS} 项未展示（JSON 模式可获取完整列表）")

    lines.extend([
        "",
        f"确认令牌 (CONFIRM_TOKEN): {token}",
        "",
        "⚠️  这是扫描报告，尚未删除任何文件。",
        "    请人工核对每一项；确认后用该令牌执行 clean 才会移入回收站。",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="工作区临时文件自检与安全清理（两阶段 + 令牌确认 + 仅回收站）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan_p = sub.add_parser("scan", help="只读扫描并输出报告与确认令牌")
    scan_p.add_argument("--root", default=".", help="工作区根目录（默认当前目录）")
    scan_p.add_argument("--json", action="store_true", help="输出 JSON（含完整列表与令牌）")
    scan_p.add_argument(
        "--exclude", default="",
        help="逗号分隔的相对路径或文件名，用户确认保留的文件（clean 时须传入相同值）",
    )

    clean_p = sub.add_parser("clean", help="校验令牌后将候选文件移入系统回收站")
    clean_p.add_argument("--token", required=True, help="scan 输出的确认令牌")
    clean_p.add_argument("--root", default=".", help="工作区根目录（须与 scan 一致）")
    clean_p.add_argument("--json", action="store_true", help="输出 JSON 结果")
    clean_p.add_argument("--dry-run", action="store_true", help="只校验令牌，不实际移动文件")
    clean_p.add_argument(
        "--exclude", default="",
        help="逗号分隔的相对路径或文件名，须与 scan 时传入的值完全一致",
    )

    args = parser.parse_args()

    try:
        root = validate_root(args.root)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    excludes = parse_excludes(args.exclude)
    candidates = apply_excludes(scan_candidates(root), excludes)
    token = compute_token(root, candidates)

    # ---------------- scan ----------------
    if args.command == "scan":
        if args.json:
            print(json.dumps({
                "root": str(root),
                "count": len(candidates),
                "token": token,
                "files": [
                    {
                        "relative": c["relative"],
                        "path": c["path"],
                        "size": c["size"],
                        "mtime": c["mtime"],
                        "rules": c["rules"],
                    }
                    for c in candidates
                ],
            }, ensure_ascii=False, indent=2))
        else:
            print(format_report(root, candidates, token))
        return 0

    # ---------------- clean ----------------
    if args.token.strip().lower() != token.lower():
        message = (
            "令牌校验失败：候选文件集合自扫描后已发生变化（或令牌错误）。"
            "为保证安全，未删除任何文件。请重新执行 scan 获取新令牌。"
        )
        if args.json:
            print(json.dumps({"ok": False, "error": message, "token_mismatch": True},
                             ensure_ascii=False))
        else:
            print(f"中止: {message}", file=sys.stderr)
        return 2

    if args.dry_run:
        result = {
            "ok": True, "dry_run": True, "token": token,
            "count": len(candidates), "moved": 0, "failed": [],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
              else f"[dry-run] 令牌有效，共 {len(candidates)} 个文件待清理（未实际移动）。")
        return 0

    if not candidates:
        result = {"ok": True, "token": token, "count": 0, "moved": 0, "failed": []}
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
              else "无需清理：没有候选文件，未做任何操作。")
        return 0

    paths = [c["path"] for c in candidates]
    moved, error = _send_to_recycle_bin(paths)
    failed = [p for p in paths if os.path.exists(p)]
    result = {
        "ok": moved == len(paths) and not failed,
        "token": token,
        "count": len(paths),
        "moved": moved,
        "failed": failed,
        "error": error,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"清理完成: {moved}/{len(paths)} 个文件已移入系统回收站。")
        for item in candidates:
            if item["path"] in failed:
                print(f"  失败: {item['relative']}")
        if error:
            print(f"  错误: {error}", file=sys.stderr)

    return 0 if result["ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
