"""Passage extraction: return only the parts of a page that answer a query.

Why this exists
---------------
Reading a full page to answer one question is the most expensive thing an
agent does. A typical documentation page is ~4k characters of which maybe 1.5k
are relevant; a Wikipedia article can be 60k. Sending the whole thing costs
tokens the model spends re-reading navigation and unrelated sections.

This module splits extracted text into blocks (headings, paragraphs, list
runs, code fences, table rows) and keeps the blocks that best match a query,
in document order, with a hard character budget.

Design notes
------------
* **Document order is preserved** in the output. Reordering passages by score
  makes the page unreadable and destroys context ("the next section").
* **Headings are sticky.** A heading is always kept alongside the passage it
  introduces, so a kept block never appears without its section title.
* **Neighbours are pulled in.** A block adjacent to a strong match is usually
  the other half of the answer (a code block after "For example:"), so
  adjacent blocks receive a partial score.
* **Never worse than nothing.** If nothing matches, the head of the document
  is returned so the caller still sees what the page is.
"""
from __future__ import annotations

import re

from .relevance import query_terms

# Blocks are split on blank lines, which is how both the markdown and text
# renderers separate structural units.
_BLOCK_SPLIT = re.compile(r"\n\s*\n")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# A heading is kept with its section even if the heading text itself is short.
_HEADING_WEIGHT = 0.5
# Neighbouring blocks get this fraction of their own score, so a code sample
# right after a matching sentence is not discarded.
_NEIGHBOUR_FACTOR = 0.45
# Smallest useful slice of an oversized block. Below this a fragment carries no
# meaning, so the block is skipped rather than returned as a stub.
_MIN_SLICE = 120


def _terms_in(text: str, terms: list[str]) -> int:
    low = text.lower()
    return sum(1 for t in terms if t in low)


def _split_blocks(text: str) -> list[str]:
    return [b for b in _BLOCK_SPLIT.split(text) if b.strip()]


def _is_heading(block: str) -> bool:
    return bool(_HEADING_RE.match(block.strip()))


def _heading_text(block: str) -> str:
    m = _HEADING_RE.match(block.strip())
    return m.group(2) if m else block.strip()


def extract_passages(text: str, query: str, *, max_chars: int = 6000,
                     max_passages: int = 12) -> dict:
    """Select the passages of ``text`` most relevant to ``query``.

    Returns ``{"text": str, "passages": int, "matched": bool,
    "total_chars": int, "kept_chars": int}``.
    """
    total_chars = len(text or "")
    if not text or not text.strip():
        return {"text": "", "passages": 0, "matched": False,
                "total_chars": 0, "kept_chars": 0}

    terms = sorted(query_terms(query))
    blocks = _split_blocks(text)
    if not terms or len(blocks) <= 1:
        # Nothing to *rank* on — but the query may still match, and a
        # single-block page longer than the budget is still truncated. Both
        # flags used to be hardcoded False here, so a one-paragraph page that
        # plainly answered the query reported passage_matched=false and
        # truncated=false even when it had been cut.
        head = text[:max_chars]
        return {"text": head, "passages": len(blocks),
                "matched": bool(terms) and _terms_in(text, terms) > 0,
                "total_chars": total_chars, "kept_chars": len(head),
                "sliced": len(head) < total_chars}

    # Score each block by distinct query terms present, normalised by length so
    # a long boilerplate paragraph cannot outscore a precise short one.
    raw: list[float] = []
    for b in blocks:
        hits = _terms_in(b, terms)
        if hits == 0:
            raw.append(0.0)
            continue
        length_penalty = 1.0 / (1.0 + max(0, len(b) - 600) / 1200.0)
        raw.append(hits * length_penalty)

    # A heading lends its score to the section that follows it, and inherits a
    # little from it, so headings stay attached to kept content.
    scored = list(raw)
    for i, b in enumerate(blocks):
        if _is_heading(b):
            following = raw[i + 1] if i + 1 < len(raw) else 0.0
            scored[i] = max(scored[i], following * _HEADING_WEIGHT)
            if i + 1 < len(scored):
                scored[i + 1] = max(scored[i + 1], raw[i + 1] + scored[i] * 0.15)

    # Neighbour bonus: half of the strongest adjacent score.
    boosted = list(scored)
    for i in range(len(scored)):
        for j in (i - 1, i + 1):
            if 0 <= j < len(scored) and scored[j] > 0:
                boosted[i] = max(boosted[i], scored[j] * _NEIGHBOUR_FACTOR)

    order = sorted(range(len(blocks)), key=lambda i: boosted[i], reverse=True)
    chosen: list[int] = []
    # Per-block character limit, used when a single block is longer than the
    # whole budget. Without it the top-ranked oversized block consumes
    # everything and every lower-ranked (but fitting) passage is dropped.
    limits: dict[int, int] = {}
    used = 0
    for i in order:
        if boosted[i] <= 0 or len(chosen) >= max_passages:
            break
        cost = len(blocks[i]) + 2
        remaining = max_chars - used
        if remaining <= 0:
            break
        if cost <= remaining:
            chosen.append(i)
            used += cost
            continue
        # This block alone would eat the rest of the budget. Give it at most
        # half of what is left so there is still room for other passages, and
        # keep scanning instead of breaking out.
        share = min(cost, max(_MIN_SLICE, remaining // 2))
        if share < _MIN_SLICE:
            continue
        limits[i] = share - 2
        chosen.append(i)
        used += share

    if not chosen:
        head = text[:max_chars]
        return {"text": head, "passages": 0, "matched": False,
                "total_chars": total_chars, "kept_chars": len(head),
                "sliced": len(head) < total_chars}

    # Headings are sticky: a kept body block must not arrive without the
    # section title that gives it context. These are added after selection, so
    # they never consume the passage count or the character budget.
    chosen_set = set(chosen)
    for i in list(chosen):
        if _is_heading(blocks[i]):
            continue
        for j in range(i - 1, -1, -1):
            if _is_heading(blocks[j]):
                if j not in chosen_set:
                    chosen.append(j)
                    chosen_set.add(j)
                break

    chosen.sort()
    parts = []
    for i in chosen:
        block = blocks[i]
        limit = limits.get(i)
        if limit is not None and len(block) > limit:
            block = block[:limit].rstrip() + " …"
        parts.append(block)
    body = "\n\n".join(parts)
    body_cut = len(body) > max_chars
    if body_cut:
        body = body[:max_chars]

    # ``sliced`` means "the caller is not receiving the whole document": either
    # blocks were dropped, or the joined body was cut. It is reported
    # explicitly rather than inferred from kept_chars < total_chars, because
    # re-joining blocks normalises whitespace and makes that comparison true
    # even when every block was kept.
    dropped = any(boosted[i] > 0 and i not in chosen_set for i in range(len(blocks)))
    return {
        "text": body,
        "passages": len(chosen),
        "matched": True,
        "total_chars": total_chars,
        "kept_chars": len(body),
        "sliced": bool(body_cut or dropped or limits),
    }
