from zotero_audio.segment import chunk_text, create_speech_plan, split_sentences


def test_sentence_split_preserves_common_abbreviations():
    assert split_sentences("Use e.g. this method. Then stop.") == [
        "Use e.g. this method.",
        "Then stop.",
    ]


def test_chunk_text_obeys_limit_at_sentence_boundaries():
    chunks = chunk_text("One short sentence. Another short sentence. A third sentence.", 35)
    assert all(len(chunk) <= 35 for chunk in chunks)
    assert " ".join(chunks) == "One short sentence. Another short sentence. A third sentence."


def test_plan_is_stable_and_preserves_page_mapping():
    structure = {
        "document": {"title": "Paper", "author": None, "publication_year": None},
        "source": {"sha256": "f" * 64},
        "structure_sha256": "e" * 64,
        "blocks": [
            {
                "id": "p0002-b0001",
                "pdf_page": 2,
                "type": "paragraph",
                "text": "A result. Another result.",
                "included_in_reading": True,
            }
        ],
    }
    first = create_speech_plan(structure, max_chars=200)
    second = create_speech_plan(structure, max_chars=200)
    assert first == second
    assert first["segments"][1]["pdf_pages"] == [2]
    assert first["segments"][1]["source_block_ids"] == ["p0002-b0001"]


def test_full_plan_keeps_long_sentence_intact_for_phoneme_budgeting():
    sentence = "This sentence " + "contains many original words " * 45 + "and ends here."
    structure = {
        "document": {"title": "Paper"}, "source": {"sha256": "a" * 64},
        "structure_sha256": "b" * 64,
        "blocks": [{"id": "p1", "type": "paragraph", "text": sentence,
                    "pdf_page": 1, "included_in_reading": True}],
    }
    plan = create_speech_plan(structure, max_chars=900)
    assert plan["segments"][1]["text"] == sentence
    assert len(plan["segments"]) == 2
