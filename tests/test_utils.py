"""Tests for utils/page_ranges.py and the FSM temp-file tracking helpers
in utils/tempfiles.py (the core fix for the "abandoned flow leaks temp
files forever" bug).
"""
import os
import pytest

from utils.page_ranges import (
    parse_page_range_group,
    parse_multi_group_ranges,
    parse_rearrange_order,
)
from utils.tempfiles import (
    new_temp_path,
    track_temp_file,
    track_temp_files,
    get_tracked_files,
    untrack_temp_files,
    cleanup_tracked_files,
    delete_path,
)
from utils.validators import sanitize_filename


def test_parse_simple_range():
    assert parse_page_range_group("1-3,5", 10) == [0, 1, 2, 4]


def test_parse_descending_range():
    assert parse_page_range_group("3-1", 10) == [2, 1, 0]


def test_parse_rejects_out_of_bounds():
    with pytest.raises(ValueError):
        parse_page_range_group("1-20", 10)


def test_parse_multi_group():
    assert parse_multi_group_ranges("1-2;3", 5) == [[0, 1], [2]]


def test_rearrange_order_valid_permutation():
    assert parse_rearrange_order("2,1,3", 3) == [1, 0, 2]


def test_rearrange_order_rejects_incomplete():
    with pytest.raises(ValueError):
        parse_rearrange_order("1,2", 3)


def test_rearrange_order_rejects_duplicate():
    with pytest.raises(ValueError):
        parse_rearrange_order("1,1,2", 3)


def test_sanitize_filename_strips_traversal():
    assert ".." not in sanitize_filename("../../etc/passwd")


def test_sanitize_filename_strips_path_separators():
    result = sanitize_filename("/etc/passwd")
    assert "/" not in result


async def test_abandoned_flow_cleans_up_tracked_files(fsm_state, tmp_path):
    """Simulates: user uploads 3 files to a Merge flow, then presses
    Cancel without finishing. base.py's cancel handler calls exactly this
    sequence -- this is the regression test for the leak bug the audit
    identified.
    """
    paths = []
    for i in range(3):
        p = new_temp_path(suffix=".pdf", base_dir=str(tmp_path))
        with open(p, "wb") as f:
            f.write(b"%PDF-1.4 fake content")
        await track_temp_file(fsm_state, p)
        paths.append(p)

    assert len(await get_tracked_files(fsm_state)) == 3
    assert all(os.path.exists(p) for p in paths)

    await cleanup_tracked_files(fsm_state)
    await fsm_state.clear()

    assert all(not os.path.exists(p) for p in paths)
    assert (await fsm_state.get_data()) == {}


async def test_completed_flow_untracks_after_explicit_cleanup(fsm_state, tmp_path):
    input_path = new_temp_path(suffix=".pdf", base_dir=str(tmp_path))
    output_path = new_temp_path(suffix=".pdf", base_dir=str(tmp_path))
    for p in (input_path, output_path):
        with open(p, "wb") as f:
            f.write(b"content")

    await track_temp_files(fsm_state, [input_path, output_path])
    assert len(await get_tracked_files(fsm_state)) == 2

    delete_path(input_path)
    delete_path(output_path)
    await untrack_temp_files(fsm_state, [input_path, output_path])

    assert (await get_tracked_files(fsm_state)) == []
    assert not os.path.exists(input_path)
    assert not os.path.exists(output_path)
