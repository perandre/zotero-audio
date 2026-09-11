from zotero_audio.article_recency import readable_article, reading_sort_key


def test_reading_policy_requires_a_known_2025_or_newer_year():
    assert not readable_article({"year": "2024"})
    assert not readable_article({"year": None})
    assert readable_article({"year": "2025-04"})
    assert readable_article({"year": 2026})


def test_reading_policy_prefers_2026_then_newer_eligible_years():
    ordered = sorted(
        [
            {"title": "2025 paper", "year": "2025"},
            {"title": "2026 paper", "year": "2026"},
            {"title": "2024 paper", "year": "2024"},
        ],
        key=reading_sort_key,
    )
    assert [item["title"] for item in ordered] == ["2026 paper", "2025 paper", "2024 paper"]
