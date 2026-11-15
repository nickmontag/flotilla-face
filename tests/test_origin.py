"""Where a model comes from: the list's origin field and how it's shown."""

import json

import pytest

from flotilla.feed import BUNDLED_FEED, FeedError, apply_local, origin_view, parse_feed

BASE = {"schema": 1, "version": 1, "issued": "2026-01-01T00:00:00+00:00", "expires": "2099-01-01T00:00:00+00:00"}


def feed_with(origin):
    m = {"repo": "a/b", "scores": {"capability": 50}}
    if origin is not None:
        m["origin"] = origin
    return parse_feed(json.dumps({**BASE, "models": [m]}).encode())


def test_origin_parsed_and_validated():
    f = feed_with({"base": "org/base", "method": "fine-tune", "data": "undisclosed", "future_key": 1})
    assert f.models[0].origin == {"base": "org/base", "method": "fine-tune", "data": "undisclosed"}
    assert feed_with(None).models[0].origin is None
    assert feed_with({"base": "org/base"}).models[0].origin["method"] == "unknown"
    for bad in ({"method": "magic"}, {"base": "not a repo"}, {"repack_of": "../x"}, {"data": "secret"}, "fine-tune"):
        with pytest.raises(FeedError):
            feed_with(bad)


def test_origin_view_flags_what_hashes_cant_check():
    v = origin_view("x/y", {"base": "org/base", "method": "fine-tune+refusal-removal", "data": "undisclosed"})
    assert v["tag"] == "fine-tuned" and v["warn"] and "hasn't released" in v["text"]
    v = origin_view("x/y", {"base": "org/base", "method": "fine-tune", "data": "public"})
    assert v["tag"] is None and "has released" in v["text"]
    v = origin_view("x/y", {"method": "unknown"})
    assert v["tag"] == "method unstated" and v["warn"]
    v = origin_view("quanter/m-GGUF", {"method": "refusal-removal", "repack_of": "maker/m"})
    assert v["tag"] == "repack" and not v["warn"] and "by quanter" in v["text"]
    v = origin_view("maker/m-GGUF", {"method": "refusal-removal", "repack_of": "maker/m"})
    assert v["tag"] is None and "same publisher" in v["text"]
    assert origin_view("x/y", None) is None


def test_rescoring_a_listed_model_keeps_its_origin(tmp_path):
    f = feed_with({"base": "org/base", "method": "fine-tune", "data": "undisclosed"})
    local = tmp_path / "my-priorities.json"
    local.write_text(json.dumps({"models": [{"repo": "a/b", "scores": {"capability": 99}}]}))
    f = apply_local(f, local, log=lambda *_: None)
    assert f.models[0].local and f.models[0].origin["method"] == "fine-tune"


def test_published_list_records_every_origin():
    feed = parse_feed(BUNDLED_FEED.read_bytes())
    by_repo = {m.repo: m for m in feed.models}
    for m in feed.models:
        assert m.origin and (m.origin.get("base") or m.origin["method"] == "original"), f"{m.repo} has no origin"
        if m.origin["method"] in ("fine-tune", "fine-tune+refusal-removal"):
            assert "data" in m.origin, f"{m.repo}: say whether the training data is public"
        src = m.origin.get("repack_of")
        # Prefer the publisher's own files: a third party's repack never outranks its listed source.
        if src in by_repo and src.split("/")[0].lower() != m.repo.split("/")[0].lower():
            assert feed.priority(m) < feed.priority(by_repo[src]), f"{m.repo} outranks its source {src}"


def test_license_name_read_when_hugging_face_says_other():
    from flotilla.sources import license_from_card

    assert license_from_card({"cardData": {"license": "other", "license_name": "OpenMDW-1.1"}}) == "openmdw-1.1"
    assert license_from_card({"cardData": {"license": "other"}}) == "other"
    assert license_from_card({"cardData": {"license": "MIT", "license_name": "ignored"}}) == "mit"
    assert license_from_card({"tags": ["license:apache-2.0"]}) == "apache-2.0"
    assert license_from_card({}) is None


def test_default_licenses_include_reviewed_custom_ones():
    from flotilla.config import Config

    assert {"glm-5.3", "qwen-community-1.0", "openmdw-1.1"} <= set(Config().licenses)


def test_official_releases_fill_in_after_at_risk_models():
    # The planner divides a file's value by 1 + its copies. An official release nobody holds must
    # still lose to an at-risk file that has a single copy, so it fills in after them.
    feed = parse_feed(BUNDLED_FEED.read_bytes())
    official = [feed.priority(m) for m in feed.models if (m.origin or {}).get("method") == "original"]
    at_risk = [feed.priority(m) for m in feed.models if (m.origin or {}).get("method") != "original"]
    assert official and at_risk
    assert max(official) * 1.0 < min(at_risk) / 2
