"""Common workflow/session-expiry handling, shared by every multi-step
tool (PDF, Image, Archive, Document, OCR, and any future module).

The problem this solves: every multi-step tool drives its flow with FSM
state plus an inline keyboard. If the FSM state is reset -- most commonly
because the user sent /start while a flow was in progress -- the old
inline keyboard is still visible in chat, but nothing matches its
callback data anymore, so pressing it just spins forever with no reply.

Previously each tool (Merge, Split, Compress, Rotate, Extract, Rearrange,
...) carried its own copy of a "stale callback" handler to paper over
this. That meant the same few lines were duplicated per tool, and any
future tool had to remember to add its own copy too.

This module centralizes that into ONE reusable handler. Every tool
registers the callback-data it owns (either exact strings or a prefix)
once, at import time, via `register_stale_callbacks(...)`. The single
handler below then answers on behalf of ANY registered tool whenever its
callback arrives with no FSM state behind it -- so tools never need to
carry (or duplicate) this logic themselves.

Usage from a tool module:

    from utils.session_manager import register_stale_callbacks

    # Prefix-based (recommended -- covers every callback the tool ever
    # emits without having to enumerate them):
    register_stale_callbacks(prefix="pdfwm:")

    # Or exact-match, for tools that prefer an explicit allow-list:
    register_stale_callbacks(exact={MERGE_CB_CANCEL, MERGE_CB_CONFIRM, ...})

The shared router itself must be included in the dispatcher (see
main.py) after the tool routers, so any handler with an actual matching
FSM state still takes priority -- this router only ever fires when the
FSM state is None.
"""
from typing import Iterable, List, Optional, Set

from aiogram import Router
from aiogram.types import CallbackQuery
from aiogram.filters import StateFilter

from core.logger import logger

router = Router()

_stale_prefixes: List[str] = []
_stale_exact: Set[str] = set()


def register_stale_callbacks(prefix: Optional[str] = None, exact: Optional[Iterable[str]] = None) -> None:
    """Register the callback-data this tool owns so the shared stale-
    session handler recognizes and answers it. Call once at module import
    time. Safe to call multiple times (e.g. once per prefix) for a tool
    that uses more than one namespace.
    """
    if prefix:
        if prefix not in _stale_prefixes:
            _stale_prefixes.append(prefix)
    if exact:
        _stale_exact.update(exact)


def _is_registered_stale_callback(data: Optional[str]) -> bool:
    if not data:
        return False
    if data in _stale_exact:
        return True
    return any(data.startswith(p) for p in _stale_prefixes)


@router.callback_query(StateFilter(None), lambda c: _is_registered_stale_callback(c.data))
async def stale_workflow_callback(query: CallbackQuery) -> None:
    """Fires for any tool's callback data once its FSM state has already
    been cleared (typically by /start resetting an in-progress flow).
    Tells the user their session expired and strips the dead keyboard so
    it can't be pressed again.
    """
    await query.answer("This session has expired. Please start again from the menu.", show_alert=True)
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception as e:
        logger.debug(f"Session manager: could not strip keyboard from stale callback message: {e}")
