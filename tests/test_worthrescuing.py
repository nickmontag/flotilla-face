"""Pirate Face's Worth Rescuing page -> list entries (an editor tool)."""

import json

import pytest

from flotilla.worthrescuing import VIA, capability_from_index, license_mismatches, parse_page, sync


def el(tag, props, key=None):
    return ["$", tag, key, props]


def page_html(archive_rows, ranked_rows) -> str:
    """A page shaped like Pirate Face's: React flight rows pushed from <script> tags, model
    cells pointing at separate rows ("$L..") that hold the link."""
    rows, n = {}, [10]

    def link(repo):
        rid = format(n[0], "x"); n[0] += 1
        rows[rid] = el("$L1", {"href": "/" + repo, "className": "worth-rescuing-model", "children": repo})
        return "$L" + rid

    archive = el("table", {"className": "lb-table worth-rescuing-table worth-rescuing-archive-table", "children": [
        el("tbody", {"children": [el("tr", {"children": [
            el("td", {"className": "worth-rescuing-model-cell", "children": link(r)}),
            el("td", {"className": "c-num", "children": "510 GB"})]}, r) for r in archive_rows]})]})
    ranked = el("table", {"className": "lb-table worth-rescuing-table worth-rescuing-capability-table", "children": [
        el("tbody", {"children": [el("tr", {"children": [
            el("td", {"className": "c-rank", "children": el("span", {"children": i + 1})}),
            el("td", {"className": "worth-rescuing-model-cell", "children": link(r)}),
            el("td", {"className": "c-num worth-rescuing-score", "children": str(score)})]}, r)
            for i, (r, score) in enumerate(ranked_rows)]})]})
    rows["2"] = el("div", {"children": [archive, ranked]})
    text = "".join(f"{k}:{json.dumps(v)}\n" for k, v in rows.items()) + "1:I[\"module\"]\n"
    half = len(text) // 2   # split across two pushes, as the real page does
    pushes = "".join(f'<script>self.__next_f.push([1,{json.dumps(part)}])</script>' for part in (text[:half], text[half:]))
    return f"<html><body><table><tr><td><template id=\"P:4\"></template></td></tr></table>{pushes}</body></html>"


def test_parse_page_reads_both_lists():
    html = page_html(["org/old-model", "org/both"], [("org/top", 46.5), ("org/both", 20.1)])
    got = {m.repo: m for m in parse_page(html)}
    assert set(got) == {"org/old-model", "org/both", "org/top"}
    assert got["org/old-model"].historic and got["org/old-model"].rank is None
    assert got["org/top"].rank == 1 and got["org/top"].index == 46.5 and not got["org/top"].historic
    assert got["org/both"].historic and got["org/both"].rank == 2


def test_parse_page_fails_loudly_when_the_page_changes():
    with pytest.raises(ValueError, match="changed"):
        parse_page("<html><body>a redesigned page</body></html>")


def test_capability_scale():
    assert capability_from_index(46.5) == 95 and capability_from_index(13.6) == 67
    assert capability_from_index(None) == 75


class PF:
    def __init__(self, torrents):
        self.torrents = torrents

    def torrent(self, repo):
        return self.torrents.get(repo)


class HF:
    def __init__(self, licenses, gone=()):
        self.licenses, self.gone = licenses, set(gone)

    def license(self, repo):
        return self.licenses.get(repo, "mit")

    def tree(self, repo, rev):
        return None if (repo, rev) in self.gone else {}


def test_sync_adds_updates_removes_only_its_own_entries():
    rev, rev2 = "a" * 40, "b" * 40
    doc = {"models": [
        {"repo": "org/listed", "scores": {}},                              # editorial: never touched
        {"repo": "org/stale", "scores": {}, "via": VIA},                   # fell off the page
        {"repo": "org/moved", "revision": rev, "scores": {}, "via": VIA},  # still on it, new commit
    ]}
    page = parse_page(page_html(["org/listed", "org/new", "org/moved", "org/kimi", "org/none", "org/squashed"],
                                [("org/new", 30.0)]))
    pf = PF({r: {"revision": rev, "torrent": "/x.torrent"} for r in ("org/new", "org/kimi", "org/squashed")}
            | {"org/moved": {"revision": rev2, "torrent": None}})
    hf = HF({"org/kimi": "kimi-k3"}, gone={("org/squashed", rev)})
    s = sync(doc, page, pf, hf, {"mit"})
    by = {e["repo"]: e for e in doc["models"]}
    assert set(by) == {"org/listed", "org/new", "org/moved"}
    assert by["org/listed"] == {"repo": "org/listed", "scores": {}}
    new = by["org/new"]
    assert new["via"] == VIA and new["revision"] == rev and new["origin"] == {"method": "original"}
    assert new["scores"]["capability"] == capability_from_index(30.0) and "#1 by capability" in new["note"]
    assert by["org/moved"]["revision"] == rev2 and ".torrent file yet" in by["org/moved"]["note"]
    assert any("org/stale" in x for x in s["removed"]) and any("org/listed" in x for x in s["kept"])
    reasons = " ".join(s["skipped"])
    assert "org/kimi: license kimi-k3" in reasons and "org/none: no Pirate Face torrent" in reasons
    assert "org/squashed: Hugging Face no longer has" in reasons
    assert sync(doc, page, pf, hf, {"mit"})["added"] == []          # running it again changes nothing
    assert sync(doc, page, pf, hf, {"mit"})["updated"] == []


def test_license_mismatch_flags_relabelled_custom_licenses():
    hf = HF({"zai/GLM": "glm-5.3", "qwen/Q": "apache-2.0", "relabel/glm": "mit", "relabel/q": "mit"})
    doc = {"models": [
        {"repo": "relabel/glm", "origin": {"base": "zai/GLM"}},                      # MIT on a custom license
        {"repo": "fixed/glm", "license": "glm-5.3", "origin": {"base": "zai/GLM"}},  # corrected in the list
        {"repo": "relabel/q", "origin": {"base": "qwen/Q"}},                         # Apache base may be relicensed
    ]}
    assert license_mismatches(doc, hf) == ["relabel/glm: mit, but its base zai/GLM is glm-5.3"]
