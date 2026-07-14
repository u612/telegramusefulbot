"""Parsing/validation for user-supplied page range specs, shared by PDF
Split / Extract / Rearrange.
"""
from typing import List


def parse_page_range_group(spec: str, total_pages: int) -> List[int]:
    """Parse one comma-separated group like '1-3,5,9-8' into a 0-indexed
    page list, preserving the order given (supports descending ranges like
    '9-8' -> [8, 7], which matters for Rearrange).
    """
    pages: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                raise ValueError(f"Invalid range '{part}'.")
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError:
                raise ValueError(f"Invalid range '{part}': not numeric.")
            if start < 1 or end < 1:
                raise ValueError(f"Page numbers must be 1 or greater (got '{part}').")
            step = 1 if start <= end else -1
            for p in range(start, end + step, step):
                if p > total_pages:
                    raise ValueError(f"Page {p} is out of range (document has {total_pages} pages).")
                pages.append(p - 1)
        else:
            try:
                p = int(part)
            except ValueError:
                raise ValueError(f"Invalid page number '{part}'.")
            if p < 1 or p > total_pages:
                raise ValueError(f"Page {p} is out of range (document has {total_pages} pages).")
            pages.append(p - 1)

    if not pages:
        raise ValueError("No pages specified.")
    return pages


def parse_multi_group_ranges(spec: str, total_pages: int, group_delim: str = ";") -> List[List[int]]:
    """Parse multiple groups separated by `;`, e.g. '1-3;4-6;7' -> three
    separate page lists. Used by Split (each group becomes one output PDF).
    """
    groups = [g.strip() for g in spec.split(group_delim) if g.strip()]
    if not groups:
        raise ValueError("No groups specified. Separate groups with ';', e.g. 1-3;4-6;7")
    return [parse_page_range_group(g, total_pages) for g in groups]


def parse_rearrange_order(spec: str, total_pages: int) -> List[int]:
    """Parse a full permutation like '3,1,2' -- every page must appear
    exactly once.
    """
    pages = parse_page_range_group(spec, total_pages)
    if len(pages) != total_pages or set(pages) != set(range(total_pages)):
        raise ValueError(
            f"Order must include every page from 1 to {total_pages} exactly once."
        )
    return pages
