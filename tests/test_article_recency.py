from zotero_audio.article_recency import readable_article, reading_scope, reading_sort_key


def test_reading_policy_is_scoped_to_viking_research_articles():
    scoped = {"year": "2024", "metadata": {"item_type": "journalArticle", "collections": ["00 Inbox"]}}
    assert reading_scope(scoped) == "viking_research"
    assert not readable_article(scoped)
    assert not readable_article({**scoped, "year": None})
    assert readable_article({**scoped, "year": "2025-04"})
    assert readable_article({**scoped, "year": 2026})
    assert readable_article({"year": "2024", "metadata": {"item_type": "book", "collections": ["Mandatory"]}})


def test_reading_policy_prefers_2026_then_newer_eligible_years():
    ordered = sorted(
        [
            {"title": "2025 paper", "year": "2025", "metadata": {"item_type": "journalArticle", "collections": ["00 Inbox"]}},
            {"title": "2026 paper", "year": "2026", "metadata": {"item_type": "journalArticle", "collections": ["00 Inbox"]}},
            {"title": "2024 course book", "year": "2024", "metadata": {"item_type": "book", "collections": ["Mandatory"]}},
        ],
        key=reading_sort_key,
    )
    assert [item["title"] for item in ordered] == ["2026 paper", "2025 paper", "2024 course book"]
