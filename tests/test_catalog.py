"""The catalog list (editor side): popular models Pirate Face's magnet index lists."""

from __future__ import annotations

import json

import pytest

from flotilla import catalog, fallback
from flotilla.feed import parse_feed
from flotilla.netsafe import FetchError

from .fakes import TRACKER
from .test_fallback import HF, LICENSES, PF, REV, base_list, doc


class PopularHF(HF):
    def popular(self, tag, limit):
        return [d for d in self.found if d.get("pipeline_tag") == tag][:limit]


def magnet(repo: str, ih: str) -> str:
    ws = f"https%3A%2F%2Fpirateface.co%2Fapi%2Fws%2F{repo.replace('/', '%2F')}%2F{REV}%2F{repo.split('/')[1]}%2F"
    return f"magnet:?xt=urn:btih:{ih}&tr={TRACKER}&ws={ws}"


def test_index_is_read_and_a_bad_copy_refused(tmp_path):
    d = tmp_path / "shards"
    d.mkdir()
    (d / "2026-11-09.txt").write_text(magnet("Org/Model-7B", "1" * 40) + "\n" + magnet("org/gone", "2" * 40) + "\n")
    (d / "removed-2026-11-16.txt").write_text("2" * 40 + "\n")
    assert catalog.read_index(d) == {"org/model-7b"}
    (d / "removed-2026-11-16.txt").write_text("not a hash\n")
    with pytest.raises(ValueError, match="can't be trusted"):
        catalog.read_index(d)
    with pytest.raises(ValueError, match="no magnet index"):
        catalog.read_index(tmp_path / "empty")


def found():
    return [
        doc("Qwen/Q-9B", 9_000_000),                                    # on the list already
        doc("Org/Model-7B", 2_000_000),
        doc("bart/Model-7B-GGUF", 4_000_000, parent="Org/Model-7B", relation="quantized", gguf=True),
        doc("Org/Model-7B-Instruct", 500_000, parent="Org/Model-7B", relation="finetune"),
        doc("Other/Small-1B", 3_000),
        doc("Other/Small-1B-abliterated", 90_000, parent="Other/Small-1B", relation="finetune"),
        doc("NotOnPF/Big", 10_000_000),
        doc("Vis/Seer-4B", 80_000, pipeline="image-text-to-text"),
    ]


def ranked():
    listed = {r.lower() for r in ("Qwen/Q-9B", "Org/Model-7B", "bart/Model-7B-GGUF", "Org/Model-7B-Instruct",
                                  "Other/Small-1B", "Other/Small-1B-abliterated", "Vis/Seer-4B")}
    hits = catalog.gather_hits(PopularHF(found()), listed, log=lambda *_: None)
    return hits, catalog.group_hits(hits)


def test_only_models_pirate_face_lists_and_no_refusal_removed_builds():
    hits, groups = ranked()
    assert "NotOnPF/Big" not in hits and "Other/Small-1B-abliterated" not in hits
    assert "Vis/Seer-4B" in hits                       # vision-language models count too
    # a third party's quantisation joins the model it quantises
    [g] = [g for g in groups if any(h.repo == "Org/Model-7B" for h in g.members)]
    assert {h.repo for h in g.members} == {"Org/Model-7B", "bart/Model-7B-GGUF"}


def test_one_entry_per_base_full_precision_first_priced_between_unlisted_and_the_rest():
    _, groups = ranked()
    d = base_list()
    pf = PF({"Org/Model-7B", "bart/Model-7B-GGUF", "Org/Model-7B-Instruct", "Other/Small-1B", "Vis/Seer-4B"})
    s = catalog.sync(d, groups, pf, HF(), LICENSES)
    mine = {e["repo"]: e for e in d["models"] if e.get("via") == catalog.VIA}
    # the fine-tune is built on a model the catalog already took: one per base
    assert set(mine) == {"Org/Model-7B", "Other/Small-1B", "Vis/Seer-4B"}
    assert any("Org/Model-7B-Instruct: already 1 catalog build of Org/Model-7B" in x for x in s["skipped"])
    assert any("Qwen/Q-9B is already on the list" in x for x in s["kept"])
    e = mine["Org/Model-7B"]
    assert e["target_replicas"] == 2 and e["revision"] == REV and e["origin"] == {"method": "unknown"}
    feed = parse_feed(json.dumps(d).encode())
    p = {m.repo: feed.priority(m) for m in feed.models}
    assert feed.unlisted_priority < p["Other/Small-1B"] < p["Org/Model-7B"] <= catalog.PRIORITY_MAX + 1e-3
    assert p["Org/Model-7B"] > catalog.PRIORITY_MIN + 0.02          # 6M downloads with its quantisation
    assert p["Other/Small-1B"] < p["Qwen/Q-9B"]                      # below the official releases


def test_resync_removes_only_its_own_entries_and_leaves_fallbacks_alone():
    _, groups = ranked()
    d = base_list()
    d["models"].append({"repo": "Other/Small-1B-abliterated", "scores": {}, "via": fallback.VIA})
    catalog.sync(d, groups, PF({"Org/Model-7B", "Other/Small-1B", "Vis/Seer-4B"}), HF(), LICENSES)
    s = catalog.sync(d, groups, PF({"Org/Model-7B"}), HF(), LICENSES)
    assert {e["repo"] for e in d["models"] if e.get("via") == catalog.VIA} == {"Org/Model-7B"}
    assert len(s["removed"]) == 2
    assert any(e["repo"] == "Other/Small-1B-abliterated" for e in d["models"])


def test_lookups_wait_out_too_many_requests():
    calls, slept = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise FetchError("https://huggingface.co/api/x", 429, "slow down", retry_after=7)
        return "ok"

    assert fallback.patient(flaky, sleep=slept.append) == "ok" and slept == [7, 7]

    def broken():
        raise FetchError("https://huggingface.co/api/x", 500, "down")

    with pytest.raises(FetchError):
        fallback.patient(broken, sleep=slept.append)


def test_a_listing_without_a_full_commit_is_skipped():
    class LoosePF(PF):
        def torrent(self, repo):
            got = super().torrent(repo)
            return {**got, "revision": "main"} if got else None

    _, groups = ranked()
    d = base_list()
    s = catalog.sync(d, groups, LoosePF({"Other/Small-1B"}), HF(), LICENSES)
    assert not [e for e in d["models"] if e.get("via") == catalog.VIA]
    assert any("pins no full commit" in x for x in s["skipped"])
