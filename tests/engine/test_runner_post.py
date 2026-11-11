"""The text a video is posted with: what the director wrote, as far as a client is given it."""

from __future__ import annotations

import json

from engine.agent.runner import clean_post, read_post


def test_a_post_is_a_title_a_description_and_hashtags_without_their_sign():
    post = clean_post({"title": "  Cold brew\nwithout the wait ", "description": "Lumabrew makes it\tin a minute.",
                       "hashtags": ["#Lumabrew", "cold brew", "#coffee-tok", "lumabrew", "", 7, "café"]})
    assert post == {"title": "Cold brew without the wait", "description": "Lumabrew makes it in a minute.",
                    "hashtags": ["Lumabrew", "coldbrew", "coffeetok", "café"]}


def test_hashtags_written_as_one_string_are_split():
    assert clean_post({"title": "A", "hashtags": "#one, #two  three"})["hashtags"] == ["one", "two", "three"]


def test_long_text_is_cut_at_a_word_and_the_list_is_capped():
    post = clean_post({"title": "word " * 60, "description": "sentence " * 200,
                       "hashtags": [f"tag{n}" for n in range(30)] + ["x" * 90]})
    assert len(post["title"]) <= 100 and post["title"].endswith("word")
    assert len(post["description"]) <= 500 and post["description"].endswith("sentence")
    assert len(post["hashtags"]) == 8


def test_nothing_to_post_with_is_no_post():
    for empty in (None, [], "text", {}, {"hashtags": ["a"]}, {"title": " ", "description": ""}):
        assert clean_post(empty) is None


def test_the_post_is_read_from_the_projects_artifacts(tmp_path):
    assert read_post(tmp_path) is None
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts" / "post.json").write_text("{not json", encoding="utf-8")
    assert read_post(tmp_path) is None
    (tmp_path / "artifacts" / "post.json").write_text(
        json.dumps({"title": "T", "description": "D", "hashtags": ["h"]}), encoding="utf-8")
    assert read_post(tmp_path) == {"title": "T", "description": "D", "hashtags": ["h"]}
