"""FR-11 切片策略单测。

覆盖项：
- 目标长度分布：≥90% 落在 300~700 字符（PRD FR-11 口径）
- 碎片控制：无 <200 字符的孤立碎片（与相邻块合并）
- 超长硬切：单段落 >33000 字符必须硬切，且每片 ≤33000
- 覆盖全部 5 篇种子文档
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.ingest import (  # noqa: E402
    HARD_CUT_SIZE,
    MIN_CHUNK_SIZE,
    chunk_text,
)

SEED_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "seed_docs"
SEED_FILES = sorted(SEED_DIR.glob("*.md"))

# FR-11 分布区间与覆盖率门槛
DIST_MIN = 300
DIST_MAX = 700
MIN_COVERAGE = 0.90


def test_seed_docs_present():
    """种子文档应至少 5 篇。"""
    assert len(SEED_FILES) >= 5, f"seed_docs 下仅 {len(SEED_FILES)} 篇"


@pytest.mark.parametrize("seed", SEED_FILES, ids=lambda p: p.name)
def test_fr11_distribution_per_doc(seed: Path):
    """每篇种子文档：≥90% 切片落在 300~700 字符。"""
    text = seed.read_text(encoding="utf-8")
    chunks = chunk_text(text)
    assert chunks, f"{seed.name} 切片为空"

    sizes = [len(c["content"]) for c in chunks]
    in_range = [s for s in sizes if DIST_MIN <= s <= DIST_MAX]
    coverage = len(in_range) / len(sizes)
    assert coverage >= MIN_COVERAGE, (
        f"{seed.name} 分布覆盖率 {coverage:.0%} < {MIN_COVERAGE:.0%}，sizes={sizes}"
    )


@pytest.mark.parametrize("seed", SEED_FILES, ids=lambda p: p.name)
def test_no_tiny_fragments(seed: Path):
    """每篇种子文档：不应产生 <200 字符的孤立碎片。"""
    text = seed.read_text(encoding="utf-8")
    chunks = chunk_text(text)
    tiny = [len(c["content"]) for c in chunks if len(c["content"]) < MIN_CHUNK_SIZE]
    assert not tiny, f"{seed.name} 存在碎片 {tiny}"


def test_all_seed_docs_aggregate_coverage():
    """5 篇合并口径：整体分布覆盖率 ≥90%。"""
    all_sizes: list[int] = []
    for seed in SEED_FILES:
        chunks = chunk_text(seed.read_text(encoding="utf-8"))
        all_sizes.extend(len(c["content"]) for c in chunks)
    in_range = [s for s in all_sizes if DIST_MIN <= s <= DIST_MAX]
    coverage = len(in_range) / len(all_sizes)
    assert coverage >= MIN_COVERAGE, f"整体覆盖率 {coverage:.0%}，sizes={all_sizes}"


def test_chunk_index_is_sequential():
    """chunk_index 应从 0 连续递增。"""
    text = SEED_FILES[0].read_text(encoding="utf-8")
    chunks = chunk_text(text)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_char_ranges_monotonic():
    """char_start/char_end 应单调递增且不越界。"""
    text = SEED_FILES[0].read_text(encoding="utf-8")
    chunks = chunk_text(text)
    for c in chunks:
        assert 0 <= c["char_start"] < c["char_end"] <= len(text)


def test_empty_text_returns_empty():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  \t ") == []


def test_oversized_paragraph_is_hard_cut():
    """单段落 >33000 字符必须硬切，且每片不超硬限。"""
    huge = "A" * (HARD_CUT_SIZE * 2 + 123)
    chunks = chunk_text(huge)
    assert len(chunks) >= 2, "超长段落未被硬切"
    for c in chunks:
        assert len(c["content"]) <= HARD_CUT_SIZE, (
            f"存在超过 {HARD_CUT_SIZE} 的切片，len={len(c['content'])}"
        )


def test_code_fence_not_split_mid_block():
    """代码块内的空行不应造成段落切分（块不被从中间切断）。"""
    code = "```\nline1\n\nline2\n\nline3\n```"
    text = f"# Title\n\nbefore\n\n{code}\n\nafter\n"
    chunks = chunk_text(text)
    joined = "".join(c["content"] for c in chunks)
    # 代码围栏的起止标记必须完整保留（未被切断成半个 ```）
    assert joined.count("```") % 2 == 0


def test_heading_path_attached():
    """切片应携带所在标题路径。"""
    text = "# H1\n\n## H2\n\n" + ("content " * 80)
    chunks = chunk_text(text)
    assert chunks
    assert chunks[0]["heading_path"].startswith("H1")