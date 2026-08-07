from __future__ import annotations

from tools.aml_locomo_manifest import convert_locomo


def test_convert_locomo_preserves_sessions_categories_and_source_evidence() -> None:
    dataset = [
        {
            "sample_id": "conv-test",
            "conversation": {
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                "session_1_date_time": "1:30 pm on 8 May, 2023",
                "session_1": [
                    {"speaker": "Alice", "dia_id": "D1:1", "text": "I moved to Tokyo."},
                    {
                        "speaker": "Bob",
                        "dia_id": "D1:02",
                        "text": "That sounds exciting.",
                        "blip_caption": "a train arriving in Tokyo",
                    },
                ],
            },
            "qa": [
                {
                    "question": "Where did Alice move?",
                    "answer": "Tokyo",
                    "evidence": ["D1:1"],
                    "category": 1,
                },
                {
                    "question": "What was shown?",
                    "answer": "A train",
                    "evidence": ["D:1:02"],
                    "category": 2,
                },
                {
                    "question": "Missing annotation",
                    "answer": "Unknown",
                    "evidence": ["D"],
                    "category": 3,
                },
            ],
        }
    ]

    manifest, stats = convert_locomo(dataset, top_k=100)

    assert stats.cases == 1
    assert stats.adds == 1
    assert stats.searches == 2
    assert stats.skipped_questions_without_evidence == 1
    case = manifest.cases[0]
    assert case.adds[0].messages[0].role == "Alice"
    assert case.adds[0].messages[0].timestamp == 1_683_552_600_000
    assert "Image description: a train arriving in Tokyo" in case.adds[0].messages[1].content
    assert case.searches[0].category == "1"
    assert case.searches[0].expected_terms == ["Tokyo"]
    assert case.searches[0].expected_evidence_terms == ["I moved to Tokyo."]
    assert case.searches[1].category == "2"
    assert case.searches[1].expected_terms == ["A train"]
    assert case.searches[1].expected_evidence_terms == [
        "That sounds exciting.\nImage description: a train arriving in Tokyo"
    ]


def test_convert_locomo_filters_categories_and_question_count() -> None:
    dataset = [
        {
            "sample_id": "conv-test",
            "conversation": {
                "session_1_date_time": "1:30 pm on 8 May, 2023",
                "session_1": [{"speaker": "Alice", "dia_id": "D1:1", "text": "Evidence."}],
            },
            "qa": [
                {"question": "Q1", "evidence": ["D1:1"], "category": 1},
                {"question": "Q2", "evidence": ["D1:1"], "category": 2},
                {"question": "Q3", "evidence": ["D1:1"], "category": 2},
            ],
        }
    ]

    manifest, _ = convert_locomo(dataset, categories={"2"}, max_questions_per_sample=1)

    assert [query.query for query in manifest.cases[0].searches] == ["Q2"]


def test_convert_locomo_can_limit_sessions_for_stage_a_smoke() -> None:
    dataset = [
        {
            "sample_id": "conv-test",
            "conversation": {
                "session_1_date_time": "1:30 pm on 8 May, 2023",
                "session_1": [{"speaker": "Alice", "dia_id": "D1:1", "text": "First evidence."}],
                "session_2_date_time": "1:30 pm on 9 May, 2023",
                "session_2": [{"speaker": "Alice", "dia_id": "D2:1", "text": "Excluded evidence."}],
            },
            "qa": [
                {"question": "Included?", "evidence": ["D1:1"], "category": 1},
                {"question": "Excluded?", "evidence": ["D2:1"], "category": 1},
            ],
        }
    ]

    manifest, stats = convert_locomo(dataset, max_sessions_per_sample=1)

    assert len(manifest.cases[0].adds) == 1
    assert [query.query for query in manifest.cases[0].searches] == ["Included?"]
    assert stats.skipped_questions_without_evidence == 1


def test_convert_locomo_adds_long_answer_keywords_as_deterministic_alternatives() -> None:
    dataset = [
        {
            "sample_id": "conv-test",
            "conversation": {
                "session_1_date_time": "1:30 pm on 8 May, 2023",
                "session_1": [{"speaker": "Alice", "dia_id": "D1:1", "text": "Evidence."}],
            },
            "qa": [
                {
                    "question": "What field?",
                    "answer": "Psychology, counseling certification",
                    "evidence": ["D1:1"],
                    "category": 3,
                }
            ],
        }
    ]

    manifest, _ = convert_locomo(dataset)

    assert manifest.cases[0].searches[0].expected_terms == [
        "Psychology",
        "counseling certification",
        "counseling",
        "certification",
    ]
