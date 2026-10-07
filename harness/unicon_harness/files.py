"""Reaching files by a relative path without being walked out of the directory
they belong to. Every path the harness follows comes from somewhere it does
not control: the plan and the task files from an organiser's repo, the
submission from a contestant, outputs.json from a step that may have run
contestant code. So a path is split into segments, `.`, `..` and empty
segments are refused, and every segment is looked at without following it:
a symbolic link anywhere is refused rather than resolved.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from unicon_harness.faults import GradingError


def inside(root: Path, relative: str, what: str) -> Path:
    """The regular file `relative` names under `root`. `what` names the file in
    the fault, for example "task file data/1.in".
    """
    return _reached(root, relative, what, folder=False)


def folder_inside(root: Path, relative: str, what: str) -> Path:
    """The directory `relative` names under `root`, reached the same way."""
    return _reached(root, relative, what, folder=True)


def tree(root: Path, what: str) -> tuple[tuple[str, Path], ...]:
    """Every regular file under the directory `root`, by its path relative to
    it with `/` between segments, in name order. A symbolic link, or anything
    but a file or a directory, anywhere under it is refused.
    """
    found: list[tuple[str, Path]] = []

    def visit(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise GradingError(f"{what} cannot be read: {exc.strerror}") from None
        for entry in entries:
            mode = entry.stat(follow_symlinks=False).st_mode
            relative = f"{prefix}{entry.name}"
            if stat.S_ISLNK(mode):
                raise GradingError(f"{what} holds a symbolic link, {relative}")
            if stat.S_ISDIR(mode):
                visit(Path(entry.path), f"{relative}/")
            elif stat.S_ISREG(mode):
                found.append((relative, Path(entry.path)))
            else:
                raise GradingError(f"{what} holds {relative}, which is not a file")

    visit(root, "")
    return tuple(found)


def _reached(root: Path, relative: str, what: str, folder: bool) -> Path:
    segments = relative.split("/")
    if relative.startswith("/") or any(s in ("", ".", "..") for s in segments):
        raise GradingError(f"{what} is not a plain relative path")
    current = root
    for index, segment in enumerate(segments):
        current = current / segment
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            raise GradingError(f"{what} does not exist") from None
        except OSError as exc:
            raise GradingError(f"{what} cannot be read: {exc.strerror}") from None
        if stat.S_ISLNK(mode):
            raise GradingError(f"{what} goes through a symbolic link")
        last = index == len(segments) - 1
        if last and not folder and not stat.S_ISREG(mode):
            raise GradingError(f"{what} is not a regular file")
        if last and folder and not stat.S_ISDIR(mode):
            raise GradingError(f"{what} is not a folder")
        if not last and not stat.S_ISDIR(mode):
            raise GradingError(f"{what} does not exist")
    return current


def read_bounded(path: Path, limit: int, what: str) -> bytes:
    """A regular file's bytes, refusing one over `limit` rather than reading it."""
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise GradingError(f"{what} cannot be read: {exc.strerror}") from None
    if len(data) > limit:
        raise GradingError(f"{what} is larger than {limit} bytes")
    return data


def copy_into(source: Path, destination: Path) -> None:
    """Copy one regular file, already checked, without following a link at the
    destination either.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination, follow_symlinks=False)


def tree_size(root: Path) -> int:
    """Bytes in regular files under `root`, links not followed, 0 when absent."""
    if not root.is_dir() or root.is_symlink():
        return 0
    total = 0
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            mode = os.lstat(Path(directory) / name)
            if stat.S_ISREG(mode.st_mode):
                total += mode.st_size
    return total
