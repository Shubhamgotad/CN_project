"""Fail-closed validation and exclusive creation for offline report files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def validate_new_output_paths(
    paths: tuple[str | Path, ...],
    *,
    protected_roots: tuple[str | Path, ...] = (),
) -> tuple[Path, ...]:
    """Resolve new report paths outside protected roots and reject symlinks."""
    if not paths:
        raise ValueError("at least one explicit report output path is required")

    resolved_roots = tuple(
        Path(root).expanduser().resolve(strict=True)
        for root in (PROJECT_ROOT, *protected_roots)
    )
    outputs = []
    for value in paths:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("report output paths must be explicit non-empty paths")
        path = Path(os.path.abspath(Path(value).expanduser()))
        for component in (path, *path.parents):
            if component.is_symlink():
                raise ValueError(
                    f"report output path must not contain symlinks: {component}"
                )
        if not path.parent.is_dir():
            raise FileNotFoundError(
                f"report output directory does not exist: {path.parent}"
            )

        destination = path.parent.resolve(strict=True) / path.name
        for root in resolved_roots:
            if destination == root or root in destination.parents:
                raise ValueError(
                    "report output paths must remain outside protected "
                    "project and evidence directories"
                )
        if destination.exists():
            raise FileExistsError(
                f"refusing to overwrite existing report output: {destination}"
            )
        outputs.append(destination)

    if len(set(outputs)) != len(outputs):
        raise ValueError("report output paths must be different")
    return tuple(outputs)


def open_exclusive_text(path: str | Path, *, newline: str | None = None) -> TextIO:
    """Create a new UTF-8 report file; fail if any actor already created it."""
    destination = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o600)
    return os.fdopen(
        descriptor,
        "w",
        encoding="utf-8",
        newline=newline,
    )
