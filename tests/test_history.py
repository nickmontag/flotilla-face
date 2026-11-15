"""Upload and download history: exact bytes per torrent, kept for months."""

import pytest

from flotilla.history import DAY, HOUR, KEEP_MINUTES, MINUTE, History

T0 = 1_800_000_000 // DAY * DAY   # a UTC midnight
A, B = "a" * 40, "b" * 40


def test_minutes_and_hours_add_up(tmp_path):
    h = History(tmp_path / "h.db")
    for i in range(120):                        # two hours, one record a minute
        h.record(T0 + i * MINUTE + 5, {A: (1000, 10), B: (500, 0)}, 60, {A: "org/a", B: "org/b"})
    q = h.query("1h", now=T0 + 2 * HOUR - 1)   # the last hour, ending in the current minute
    assert q["bucket"] == MINUTE and len(q["points"]) == 60
    assert q["up"] == 60 * 1500 and q["down"] == 60 * 10 and q["online"] == 60 * 60
    q = h.query("7d", now=T0 + 2 * HOUR)        # hourly rows
    assert q["bucket"] == HOUR and q["up"] == 120 * 1500
    assert [m["repo"] for m in q["models"]] == ["org/a", "org/b"]
    assert q["models"][0] == {"repo": "org/a", "up": 120 * 1000, "down": 120 * 10}


def test_filter_by_model_sums_every_torrent_of_that_repo(tmp_path):
    h = History(tmp_path / "h.db")
    h.record(T0 + 30, {A: (100, 0), B: (7, 0)}, 60, {A: "org/m", B: "org/other"})
    h.record(T0 + 90, {"c" * 40: (50, 5)}, 60, {"c" * 40: "org/m"})   # same model, another revision
    q = h.query("1h", repo="org/m", now=T0 + 120)
    assert q["up"] == 150 and q["down"] == 5
    assert q["online"] == 120                    # online is the node's, whatever the filter
    assert h.query("1h", repo="org/none", now=T0 + 120)["up"] == 0


def test_gaps_show_as_offline_not_zero(tmp_path):
    h = History(tmp_path / "h.db")
    h.record(T0 + 10, {A: (100, 0)}, 60, {A: "org/a"})
    h.record(T0 + 50 * MINUTE, {A: (100, 0)}, 60, {A: "org/a"})
    pts = h.query("1h", now=T0 + HOUR - 1)["points"]
    online = [p[3] for p in pts]
    assert online[0] == 60 and online[50] == 60 and sum(1 for o in online if o == 0) == 58


def test_days_follow_the_viewers_time_zone(tmp_path):
    h = History(tmp_path / "h.db")
    # 23:30 UTC is 18:30 in New York (UTC-5): the same local day as 13:00 UTC.
    h.record(T0 + 13 * HOUR, {A: (1, 0)}, 60, {A: "org/a"})
    h.record(T0 + 23 * HOUR + 1800, {A: (2, 0)}, 60, {A: "org/a"})
    utc = [p for p in h.query("90d", now=T0 + 2 * DAY)["points"] if p[1]]
    ny = [p for p in h.query("90d", tz_offset=-5 * HOUR, now=T0 + 2 * DAY)["points"] if p[1]]
    assert len(utc) == 1 and utc[0][1] == 3
    assert len(ny) == 1 and ny[0][0] == T0 + 5 * HOUR      # local midnight, in UTC seconds


def test_minutes_pruned_after_three_days_hours_kept(tmp_path):
    h = History(tmp_path / "h.db")
    h.record(T0, {A: (100, 0)}, 60, {A: "org/a"})
    h.record(T0 + KEEP_MINUTES + 2 * HOUR, {A: (1, 0)}, 60)
    n_min = h.db.execute("SELECT COUNT(*) FROM traffic WHERE res = ? AND t = ?", (MINUTE, T0)).fetchone()[0]
    n_hour = h.db.execute("SELECT COUNT(*) FROM traffic WHERE res = ? AND t = ?", (HOUR, T0)).fetchone()[0]
    assert n_min == 0 and n_hour == 2           # the node row and the torrent's row
    assert h.query("all", now=T0 + 5 * DAY)["up"] == 101


def test_history_survives_a_restart(tmp_path):
    h = History(tmp_path / "h.db")
    h.record(T0 + 10, {A: (100, 0)}, 60, {A: "org/a"})
    h.close()
    assert History(tmp_path / "h.db").query("24h", now=T0 + HOUR)["up"] == 100


def test_bad_range_refused(tmp_path):
    with pytest.raises(ValueError):
        History(tmp_path / "h.db").query("5y")


def test_engine_counts_exact_bytes_and_restarted_torrents(tmp_path):
    pytest.importorskip("libtorrent")
    from flotilla.config import Config, State
    from flotilla.engine import Engine

    class Status:
        def __init__(self, u, d):
            self.total_payload_upload, self.total_payload_download = u, d

    class Handle:
        def __init__(self):
            self.u = self.d = 0

        def status(self):
            return Status(self.u, self.d)

    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    h = Handle()
    eng.handles = {A: h}
    eng.run_started = T0 - 30                    # the node started 30 s before the first sample
    eng.state.holdings[A] = {"repo": "org/a", "files": {}}
    now = T0
    eng.sample(now)                              # first sample: nothing moved yet
    h.u, h.d = 5000, 200
    eng.sample(now + 30)                         # under a minute: skipped
    eng.sample(now + 60)
    h.u = 1000                                   # torrent re-added: its counter restarted
    eng.sample(now + 120)
    q = eng.history.query("1h", now=now + 180)
    assert q["up"] == 6000 and q["down"] == 200 and q["models"][0]["repo"] == "org/a"
    assert q["online"] == 30 + 60 + 60           # the first sample counts the time since startup
    eng.handles = {}
    eng.shutdown()


def test_samples_fill_every_minute(tmp_path):
    """The status loop runs every 10 s plus a bit. Sampling "60 s after the last one" then
    lands every ~66 s and skips a minute now and then, which the chart shows as not seeding."""
    pytest.importorskip("libtorrent")
    from flotilla.config import Config, State
    from flotilla.engine import Engine

    cfg = Config()
    cfg.folder, cfg.listen_port = tmp_path / "seeds", 0
    eng = Engine(cfg, State(tmp_path / "state"), log=lambda *_: None, listen_host="127.0.0.1", dht=False)
    eng.handles = {}
    eng.run_started = T0 - 30
    t = T0 + 0.5
    while t < T0 + HOUR:
        eng.sample(t)
        t += 11.0                                # a slow status loop: 10 s plus the alert wait
    q = eng.history.query("1h", now=T0 + HOUR - 1)
    assert all(p[3] > 0 for p in q["points"]), [p for p in q["points"] if not p[3]]
    eng.shutdown()
