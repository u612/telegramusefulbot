"""Temporary file management with automatic cleanup.

Two things live here:
1. `TempFileManager` / `create_temp_file` -- low level helpers for creating
   and deleting temp paths on disk.
2. `track_temp_file(s)` / `cleanup_tracked_files` -- FSM-state-aware helpers.
   Every handler that downloads a user's file into a temp path during a
   multi-step flow MUST call `track_temp_file(state, path)` right after
   creating it. This is what lets base.py's Back/Home/Cancel handlers (and
   /start) reliably delete any file left behind when a user abandons a flow
   partway through, instead of leaking it forever.
"""
import os
import uuid
import shutil
from contextlib import contextmanager
from typing import Optional, Generator, List

from aiogram.fsm.context import FSMContext

from core.config import settings
from core.constants import STATE_TEMP_FILES_KEY
from core.logger import logger


def delete_path(path: str) -> None:
    """Best-effort delete of a file or directory. Never raises."""
    try:
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)
            logger.debug(f"Deleted temp file: {path}")
        elif os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            logger.debug(f"Deleted temp dir: {path}")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Failed to delete {path}: {e}")


def delete_paths(paths: List[str]) -> None:
    for p in paths:
        delete_path(p)


def new_temp_path(suffix: str = "", prefix: str = "tmp_", base_dir: Optional[str] = None) -> str:
    """Generate a fresh temp file path (parent dir guaranteed to exist)
    without registering it anywhere for auto-cleanup. Use this in service
    functions that produce an output file the caller needs to persist
    beyond the current function call (e.g. to send to the user) -- the
    caller is responsible for tracking/deleting it (see track_temp_file /
    delete_path).
    """
    base = base_dir or settings.TEMP_FOLDER
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{prefix}{uuid.uuid4().hex}{suffix}")


def new_temp_dir(prefix: str = "tmpdir_", base_dir: Optional[str] = None) -> str:
    """Same as new_temp_path but creates and returns a directory (e.g. for
    archive extraction workspaces)."""
    base = base_dir or settings.TEMP_FOLDER
    path = os.path.join(base, f"{prefix}{uuid.uuid4().hex}")
    os.makedirs(path, exist_ok=True)
    return path


class TempFileManager:
    """Manage a batch of temporary files with automatic cleanup.

    Use as a context manager for a single unit of work:

        with TempFileManager() as tmp:
            path = tmp.create_file(suffix=".pdf")
            ...
        # everything created via tmp.create_file()/tmp.register() is deleted here
    """

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir or settings.TEMP_FOLDER
        os.makedirs(self.base_dir, exist_ok=True)
        self._files: List[str] = []

    def create_file(self, suffix: str = "", prefix: str = "tmp_") -> str:
        """Reserve a new temp file path (does not create the file itself;
        the caller is expected to write to it) and register it for cleanup.
        """
        filename = f"{prefix}{uuid.uuid4().hex}{suffix}"
        path = os.path.join(self.base_dir, filename)
        self._files.append(path)
        return path

    def create_dir(self, prefix: str = "tmpdir_") -> str:
        """Create and register a temp directory (e.g. for archive extraction)."""
        dirname = f"{prefix}{uuid.uuid4().hex}"
        path = os.path.join(self.base_dir, dirname)
        os.makedirs(path, exist_ok=True)
        self._files.append(path)
        return path

    def register(self, path: str) -> None:
        """Register an already-existing path for cleanup."""
        if path not in self._files:
            self._files.append(path)

    def cleanup(self, path: Optional[str] = None) -> None:
        """Delete a specific registered path, or every registered path."""
        if path:
            if path in self._files:
                self._files.remove(path)
            delete_path(path)
        else:
            delete_paths(self._files)
            self._files.clear()

    def __enter__(self) -> "TempFileManager":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()


@contextmanager
def create_temp_file(suffix: str = "") -> Generator[str, None, None]:
    """Context manager for a single temp file path that is always deleted
    on exit, success or failure.
    """
    manager = TempFileManager()
    path = manager.create_file(suffix)
    try:
        yield path
    finally:
        manager.cleanup(path)


# --- FSM-state-aware tracking (fixes the "leak on cancel" class of bugs) ---

async def track_temp_file(state: FSMContext, path: str) -> None:
    """Record a temp file/dir path in FSM state so it can be swept up if the
    user abandons the current flow (Back/Home/Cancel/new /start).
    """
    data = await state.get_data()
    tracked: List[str] = list(data.get(STATE_TEMP_FILES_KEY, []))
    if path not in tracked:
        tracked.append(path)
    await state.update_data(**{STATE_TEMP_FILES_KEY: tracked})


async def track_temp_files(state: FSMContext, paths: List[str]) -> None:
    data = await state.get_data()
    tracked: List[str] = list(data.get(STATE_TEMP_FILES_KEY, []))
    for p in paths:
        if p not in tracked:
            tracked.append(p)
    await state.update_data(**{STATE_TEMP_FILES_KEY: tracked})


async def untrack_temp_files(state: FSMContext, paths: List[str]) -> None:
    """Remove paths from tracking after they've been deleted successfully,
    so a later cleanup pass doesn't try to delete them again.
    """
    data = await state.get_data()
    tracked: List[str] = list(data.get(STATE_TEMP_FILES_KEY, []))
    remaining = [p for p in tracked if p not in paths]
    await state.update_data(**{STATE_TEMP_FILES_KEY: remaining})


async def get_tracked_files(state: FSMContext) -> List[str]:
    """Return the list of temp paths currently tracked in FSM state, in the
    order they were added. Handy for multi-file-upload flows (Merge,
    Image->PDF) where the tracked list *is* the accumulated input list.
    """
    data = await state.get_data()
    return list(data.get(STATE_TEMP_FILES_KEY, []))


async def cleanup_tracked_files(state: FSMContext) -> None:
    """Delete every temp path tracked in FSM state for the current user.
    Safe to call even if nothing was tracked. Does NOT clear the rest of
    the FSM state -- call `state.clear()` separately (this is usually
    called immediately before it).
    """
    data = await state.get_data()
    tracked: List[str] = list(data.get(STATE_TEMP_FILES_KEY, []))
    if tracked:
        logger.debug(f"Cleaning up {len(tracked)} tracked temp path(s) on flow exit")
        delete_paths(tracked)
