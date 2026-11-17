"""The fallback list (editor side) and Pirate Face's new-torrents feed (node side)."""

from __future__ import annotations

import json

import pytest

from flotilla import fallback
from flotilla.candidates import gather
from flotilla.config import Config
from flotilla.feed import FeedModel, neutral_feed, parse_feed
from flotilla.netsafe import FetchError
from flotilla.sources import HFFile, parse_new_torrents

from .fakes import FakeHF, FakePF, FakeRepo, make_repo

REV = "a" * 40
LICENSES = {"mit", "apache-2.0", "qwen-community-1.0"}


def doc(repo, downloads=0, parent=None, relation=None, license="apache-2.0", pipeline="text-generation",
        gguf=False, tags=()):
    d = {"id": repo, "downloads": downloads, "likes": 0, "cardData": {"license": license},
         "pipeline_tag": pipeline, "tags": list(tags)}
    if parent:
        d["baseModels"] = {"relation": relation, "models": [{"id": parent}]}
    if gguf:
        d["gguf"] = {"total": 1}
    else:
        d["safetensors"] = {"total": 1}
    return d


class HF:
    def __init__(self, found=(), infos=None, licenses=None, gone=()):
        self.found, self.infos, self.licenses, self.gone = list(found), infos or {}, licenses or {}, set(gone)

    def search(self, term, limit):
        return [d for d in self.found if term in d["id"].lower()]

    def model_info(self, repo):
        return self.infos.get(repo)

    def license(self, repo):
        return self.licenses.get(repo)

    def tree(self, repo, rev):
        return None if repo in self.gone else {"model.safetensors": HFFile(2_000_000_000, "0" * 64, None)}


class PF:
    def __init__(self, repos, no_file=()):
        self.repos, self.no_file = set(repos), set(no_file)
        self.asked = []

    def torrent(self, repo):
        self.asked.append(repo)
        if repo not in self.repos:
            return None
        return {"revision": REV, "magnet": "magnet:?xt=urn:btih:" + "b" * 40,
                "torrent": None if repo in self.no_file else "/api/torrents/x.torrent"}


def groups_for(*docs, infos=None):
    return fallback.group_hits(fallback.gather_hits(HF(docs, infos), log=lambda *_: None))


# ---------------------------------------------------------------- finding and grouping


def test_only_language_models_and_no_adapters():
    assert fallback.hit_from(doc("a/x-abliterated"))
    assert fallback.hit_from(doc("a/x-abliterated", pipeline=None, gguf=True))
    assert fallback.hit_from(doc("a/vl-abliterated", pipeline="image-text-to-text"))
    assert not fallback.hit_from(doc("a/img-uncensored", pipeline="text-to-image"))
    assert not fallback.hit_from(doc("a/lora-uncensored", pipeline=None, tags=["lora"]))
    assert not fallback.hit_from(doc("a/x-uncensored", parent="b/x", relation="adapter"))


def test_quantisations_join_their_source_and_find_the_base():
    groups = groups_for(
        doc("huihui/Q-9B-abliterated", 1000, parent="Qwen/Q-9B", relation="finetune"),
        doc("mrader/Q-9B-abliterated-GGUF", 500_000, parent="huihui/Q-9B-abliterated", relation="quantized",
            gguf=True),
        doc("huihui/Q-9B-abliterated-GGUF", 90_000, parent="Qwen/Q-9B", relation="quantized", gguf=True),
        doc("other/Q-9B-heretic", 20_000, parent="Qwen/Q-9B", relation="finetune"),
    )
    assert len(groups) == 2
    big, small = groups
    assert {h.repo for h in big.members} == {"huihui/Q-9B-abliterated", "mrader/Q-9B-abliterated-GGUF",
                                             "huihui/Q-9B-abliterated-GGUF"}
    assert big.downloads == 591_000 and big.base == "Qwen/Q-9B"
    assert big.preference()[0].repo == "huihui/Q-9B-abliterated"   # full precision first
    assert small.members[0].repo == "other/Q-9B-heretic"


def test_source_of_a_popular_quantisation_is_looked_up():
    full = doc("huihui/Q-4B-abliterated", 10, parent="Qwen/Q-4B", relation="finetune", pipeline=None)
    groups = groups_for(
        doc("mrader/Q-4B-abliterated-GGUF", 100_000, parent="huihui/Q-4B-abliterated", relation="quantized",
            gguf=True),
        infos={"huihui/Q-4B-abliterated": full},
    )
    assert [h.repo for h in groups[0].preference()] == ["huihui/Q-4B-abliterated", "mrader/Q-4B-abliterated-GGUF"]
    assert groups[0].base == "Qwen/Q-4B"


def test_popularity_scale():
    assert fallback.popularity(0) == 0 and fallback.popularity(1000) == pytest.approx(0, abs=1e-3)
    assert fallback.popularity(10**6) == pytest.approx(1, abs=1e-3) and fallback.popularity(10**9) == 1


# ---------------------------------------------------------------- choosing entries


def base_list(**extra):
    return {"schema": 1, "version": 1, "issued": "2026-01-01T00:00:00+00:00", "expires": "2027-01-01T00:00:00+00:00",
            "defaults": {"target_replicas": 5, "unlisted_priority": 0.15},
            "models": [{"repo": "Qwen/Q-9B", "scores": {"capability": 72, "cyber": 40, "takedown_risk": 35},
                        "boost": 0.45, "origin": {"method": "original"}},
                       {"repo": "curated/Hand-Picked-abliterated", "scores": {"capability": 80, "cyber": 80,
                                                                              "takedown_risk": 80}}],
            **extra}


def test_prefers_full_precision_falls_back_to_a_quantisation():
    groups = groups_for(
        doc("huihui/Q-9B-abliterated", 1000, parent="Qwen/Q-9B", relation="finetune"),
        doc("mrader/Q-9B-abliterated-GGUF", 500_000, parent="huihui/Q-9B-abliterated", relation="quantized",
            gguf=True),
    )
    d = base_list()
    s = fallback.sync(d, groups, PF({"huihui/Q-9B-abliterated", "mrader/Q-9B-abliterated-GGUF"}),
                      HF(), LICENSES)
    [e] = [e for e in d["models"] if e.get("via") == fallback.VIA]
    assert e["repo"] == "huihui/Q-9B-abliterated" and e["revision"] == REV and len(s["added"]) == 1
    assert e["origin"] == {"method": "refusal-removal", "base": "Qwen/Q-9B"}
    assert e["scores"]["capability"] == 72          # the base's score on the list

    d = base_list()
    fallback.sync(d, groups, PF({"mrader/Q-9B-abliterated-GGUF"}), HF(), LICENSES)
    [e] = [e for e in d["models"] if e.get("via") == fallback.VIA]
    assert e["repo"] == "mrader/Q-9B-abliterated-GGUF"
    assert e["origin"]["repack_of"] == "huihui/Q-9B-abliterated"


def test_priority_sits_below_the_hand_picked_list_and_rises_with_downloads():
    groups = groups_for(doc("a/Q-9B-abliterated", 5_000_000, parent="Qwen/Q-9B", relation="finetune"),
                        doc("b/Q-9B-uncensored", 2_000, parent="Qwen/Q-9B", relation="finetune"))
    d = base_list()
    fallback.sync(d, groups, PF({"a/Q-9B-abliterated", "b/Q-9B-uncensored"}), HF(), LICENSES)
    feed = parse_feed(json.dumps(d).encode())
    p = {m.repo: feed.priority(m) for m in feed.models}
    assert p["curated/Hand-Picked-abliterated"] > 2 * p["a/Q-9B-abliterated"]
    assert p["a/Q-9B-abliterated"] > p["b/Q-9B-uncensored"] > feed.unlisted_priority
    assert p["a/Q-9B-abliterated"] < 0.3


def test_caps_per_base_skips_listed_and_denied_and_unpublished():
    groups = groups_for(
        doc("a/Q-9B-abliterated", 900, parent="Qwen/Q-9B", relation="finetune"),
        doc("b/Q-9B-heretic", 800, parent="Qwen/Q-9B", relation="finetune"),
        doc("c/Q-9B-uncensored", 700, parent="Qwen/Q-9B", relation="finetune"),
        doc("curated/Hand-Picked-abliterated", 5000),
        doc("bad/Other-abliterated", 600),
        doc("late/Other-heretic", 500),
    )
    d = base_list(deny=["bad/Other-abliterated"])
    pf = PF({"a/Q-9B-abliterated", "b/Q-9B-heretic", "c/Q-9B-uncensored", "bad/Other-abliterated",
             "late/Other-heretic"}, no_file={"late/Other-heretic"})
    s = fallback.sync(d, groups, pf, HF(), LICENSES)
    mine = [e["repo"] for e in d["models"] if e.get("via") == fallback.VIA]
    assert mine == ["a/Q-9B-abliterated", "b/Q-9B-heretic"]
    text = "\n".join(s["skipped"] + s["kept"])
    assert "already 2 fallback builds of Qwen/Q-9B" in text
    assert "curated/Hand-Picked-abliterated is already on the list" in text
    assert "deny" in text and ".torrent file" in text


def test_relabelled_licenses_use_the_base_or_are_skipped():
    groups = groups_for(doc("a/Q-9B-abliterated", 900, parent="Qwen/Q-9B", relation="finetune"),
                        doc("b/NC-abliterated", 800, parent="lab/NC", relation="finetune"))
    d = base_list()
    s = fallback.sync(d, groups, PF({"a/Q-9B-abliterated", "b/NC-abliterated"}),
                      HF(licenses={"Qwen/Q-9B": "qwen-community-1.0", "lab/NC": "cc-by-nc-4.0"}), LICENSES)
    [e] = [e for e in d["models"] if e.get("via") == fallback.VIA]
    assert e["repo"] == "a/Q-9B-abliterated" and e["license"] == "qwen-community-1.0"
    assert any("lab/NC is cc-by-nc-4.0" in x for x in s["skipped"])


def test_resync_updates_and_removes_only_its_own_entries():
    one = groups_for(doc("a/Q-9B-abliterated", 900, parent="Qwen/Q-9B", relation="finetune"),
                     doc("b/Other-heretic", 800))
    d = base_list()
    fallback.sync(d, one, PF({"a/Q-9B-abliterated", "b/Other-heretic"}), HF(), LICENSES)
    s = fallback.sync(d, one[:1], PF({"a/Q-9B-abliterated"}), HF(), LICENSES)
    assert s["added"] == [] and s["updated"] == []
    assert s["removed"] == ["b/Other-heretic (no longer among the top 40)"]
    assert [e["repo"] for e in d["models"]] == ["Qwen/Q-9B", "curated/Hand-Picked-abliterated", "a/Q-9B-abliterated"]


def test_gone_from_hugging_face_is_skipped():
    groups = groups_for(doc("a/X-abliterated", 900))
    d = base_list()
    s = fallback.sync(d, groups, PF({"a/X-abliterated"}), HF(gone={"a/X-abliterated"}), LICENSES)
    assert not [e for e in d["models"] if e.get("via") == fallback.VIA]
    assert "no longer has commit" in s["skipped"][0]


# ---------------------------------------------------------------- Pirate Face's new-torrents feed (nodes)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>new torrents</title>
<item><title>a/fresh</title><guid isPermaLink="false">%s</guid><description>0.3 GB, 1 seeders</description></item>
<item><title>b/fresher</title><guid isPermaLink="false">%s</guid><description>12.5 GB, 0 seeders</description></item>
<item><title>../etc</title><guid>%s</guid><description>1 GB, 0 seeders</description></item>
<item><title>c/no-hash</title><guid>not-a-hash</guid></item>
</channel></rss>""" % (b"1" * 40, b"2" * 40, b"3" * 40)


def test_new_torrents_feed_parsed_and_checked():
    items = parse_new_torrents(RSS)
    assert items == [{"repo": "a/fresh", "infohash": "1" * 40, "gb": 0.3, "seeders": 1},
                     {"repo": "b/fresher", "infohash": "2" * 40, "gb": 12.5, "seeders": 0}]


def test_new_torrents_feed_with_entities_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel></channel></rss>'
    with pytest.raises(FetchError):
        parse_new_torrents(evil)
    with pytest.raises(FetchError):
        parse_new_torrents(b"<html>a web page</html")


def test_unlisted_models_are_capped_on_top_of_the_list():
    listed = [FakeRepo(f"list/m{i}", make_repo(1, 1000, seed=i)) for i in range(3)]
    extra = [FakeRepo(f"new/m{i}", make_repo(1, 1000, seed=10 + i)) for i in range(4)]
    pf, hf = FakePF(listed + extra), FakeHF(listed + extra)
    pf.new_torrents = lambda: [{"repo": r.repo, "infohash": r.infohash, "gb": 1, "seeders": i % 2}
                               for i, r in enumerate(extra)]
    feed = neutral_feed()
    feed.models = [FeedModel(r.repo, {"capability": 50}) for r in listed]
    c = Config()
    c.max_candidates = 2
    cands, rejected = gather(c, feed, pf, hf, set(), log=lambda *_: None)
    repos = [x.repo for x in cands]
    assert repos[:3] == ["list/m0", "list/m1", "list/m2"]      # the whole list, though it's over the cap
    assert repos[3:] == ["new/m0", "new/m2"]                    # then the least-seeded new torrents, up to the cap
    assert all(x.priority == feed.unlisted_priority for x in cands[3:])
