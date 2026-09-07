from zotero_audio.podcast import build_markdown_transcript


def test_markdown_rejoins_segments_without_merging_source_paragraphs():
    plan = {"segments": [
        {"kind": "heading", "text": "Abstract.", "section": "Abstract"},
        {"kind": "body", "text": "The first", "section": "Abstract", "source_block_ids": ["a"]},
        {"kind": "body", "text": "sentence (Smith et al., 2024).", "section": "Abstract", "source_block_ids": ["a"]},
        {"kind": "body", "text": "Next paragraph.", "section": "Abstract", "source_block_ids": ["b"]},
    ]}
    assert build_markdown_transcript(plan, "Paper") == (
        "# Paper\n\nAbstract.\n\nThe first sentence (Smith et al., 2024).\n\nNext paragraph.\n"
    )
