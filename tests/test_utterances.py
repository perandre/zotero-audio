import pytest
from zotero_audio.audio import plan_kokoro_utterances


def test_sentences_over_160_characters_remain_intact():
    text = ('It is widely accepted that the impact of Generative Artificial Intelligence '
            '(GenAI) has been nothing short of transformational, with tangible impacts '
            'on industry, education, healthcare and government.')
    assert len(text) > 160
    assert [u['text'] for u in plan_kokoro_utterances(text, lambda t: t)] == [text]


def test_actual_phoneme_length_triggers_clause_split_without_losing_words():
    text = 'First clause is here, second clause follows.'
    parts = plan_kokoro_utterances(text, lambda t: t * 3, 75)
    assert len(parts) == 2
    assert ' '.join(p['text'] for p in parts) == text
    assert all(p['phoneme_count'] <= 75 for p in parts)
    assert parts[0]['text'].endswith(',')


def test_oversized_word_is_rejected_not_truncated():
    with pytest.raises(ValueError, match='refusing to truncate'):
        plan_kokoro_utterances('word', lambda t: 'a' * 511)


def test_citations_abbreviations_and_numbers_are_preserved():
    text = 'Dr. Smith et al. measured 3.14 units (2024). Next sentence.'
    parts = plan_kokoro_utterances(text, lambda t: t)
    assert len(parts) == 2
    assert ' '.join(p['text'] for p in parts) == text
