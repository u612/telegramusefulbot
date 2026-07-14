"""Shared pytest fixtures."""
import os
import pytest
from reportlab.pdfgen import canvas
from PIL import Image

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey


@pytest.fixture
def fsm_state():
    """A real aiogram FSMContext backed by in-memory storage, for testing
    the temp-file tracking helpers in utils/tempfiles.py against the
    actual library rather than a hand-rolled stand-in.
    """
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)
    return FSMContext(storage=storage, key=key)


@pytest.fixture
def make_pdf(tmp_path):
    def _make(name: str = "test.pdf", pages: int = 1, text: str = "test") -> str:
        path = str(tmp_path / name)
        c = canvas.Canvas(path)
        for i in range(pages):
            c.drawString(100, 750, f"{text} page {i + 1}")
            c.showPage()
        c.save()
        return path
    return _make


@pytest.fixture
def make_image(tmp_path):
    def _make(name: str = "test.png", size=(300, 200), color=(255, 0, 0), mode="RGB") -> str:
        path = str(tmp_path / name)
        Image.new(mode, size, color).save(path)
        return path
    return _make
  
