"""GitHub Actions standby: silent while the laptop lives, reminders when it is off, never twice."""
import os
import pathlib
import shutil
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
pytest.importorskip("bs4")
if not shutil.which("openssl"):
    pytest.skip("openssl is needed for the sealed state files", allow_module_level=True)

from papaya import standby
from papaya.db import DB
from papaya.parser import Item, RIYADH

KEY = "k" * 40
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=RIYADH)


class Release:
    """A dict standing in for the GitHub release assets (and the repo variable)."""
    def __init__(self):
        self.files, self.stamps, self.vars, self.n, self.gets = {}, {}, {}, 0, 0
    def put(self, name, data):
        self.n += 1; self.files[name] = data; self.stamps[name] = f"t{self.n}"
    def get(self, name):
        self.gets += 1; return self.files.get(name)
    def stamp(self, name):
        return self.stamps.get(name)
    def set_var(self, name, value):
        self.vars[name] = value; return True


class Rec:
    def __init__(self): self.sent = []
    def send(self, chat, text, markup=None, silent=False):
        self.sent.append((chat, text)); return True


def laptop_db(path, due_in_hours=5):
    """A laptop database: one linked phone, already baselined, one open assignment."""
    db = DB(str(path))
    db.set_chat("papaya01", 777); db.mark_baselined("papaya01")
    it = Item("assignment", "a1", "واجب الأحياء", "الأحياء", NOW + timedelta(hours=due_in_hours), "مفتوح", "x/Solve/1")
    db.upsert_items("papaya01", [it], NOW)
    db.record_sent("papaya01", "assignment", "a1", "new", NOW)        # the laptop already announced it
    return db


def beat(minutes_ago):
    return str(int((NOW - timedelta(minutes=minutes_ago)).timestamp()))


def test_seal_roundtrip_wrong_key_and_tampering():
    blob = standby.seal(b"hello" * 1000, KEY)
    assert b"hello" not in blob and standby.unseal(blob, KEY) == b"hello" * 1000
    with pytest.raises(standby.StateError):
        standby.unseal(blob, "z" * 40)
    bad = bytearray(blob); bad[-5] ^= 1
    with pytest.raises(standby.StateError):
        standby.unseal(bytes(bad), KEY)
    with pytest.raises(standby.StateError):
        standby.seal(b"x", "short")


def test_laptop_alive_means_actions_does_nothing(tmp_path):
    rel = Release(); rec = Rec(); told = []
    out = standby.run_standby(str(tmp_path / "a.db"), rec, told.append, rel, NOW, beat(10), key=KEY)
    assert "alive" in out and rel.gets == 0 and not rec.sent and not told


def test_no_beat_ever_means_nothing_to_do(tmp_path):
    out = standby.run_standby(str(tmp_path / "a.db"), Rec(), lambda t: None, Release(), NOW, "", key=KEY)
    assert "never reported" in out


def test_silent_laptop_reminders_go_out_once_and_the_laptop_learns_of_them(tmp_path):
    rel = Release()
    laptop = tmp_path / "laptop.db"; db = laptop_db(laptop)
    rel.put(standby.STATE_ASSET, standby.seal(standby.snapshot(str(laptop)), KEY))

    rec, told = Rec(), []                          # laptop silent for 3 hours -> Actions takes over
    work = str(tmp_path / "actions.db")
    out = standby.run_standby(work, rec, told.append, rel, NOW, beat(180), key=KEY)
    assert "sent 1 reminder" in out
    assert len(rec.sent) == 1 and rec.sent[0][0] == 777 and "واجب الأحياء" in rec.sent[0][1]
    assert len(told) == 1 and "اللابتوب ساكت" in told[0]

    # the next scheduled run, laptop still silent: nothing again, and no second outage notice
    out = standby.run_standby(str(tmp_path / "actions2.db"), rec, told.append, rel, NOW + timedelta(hours=4),
                              beat(180), key=KEY)
    assert "sent 0" in out and len(rec.sent) == 1
    assert len(told) == 1                          # same outage: no second notice

    # the laptop comes back: it merges what Actions sent, so it will not repeat the reminder
    standby._last.update(beat=0.0, push=0.0, pull=0.0)
    did = standby.tick(str(laptop), rel, now=NOW.timestamp(), key=KEY)
    assert "beat" in did and any(d.startswith("merged:") for d in did) and "push" in did
    assert rel.vars[standby.BEAT_VAR] == str(int(NOW.timestamp()))
    assert ("assignment", "a1", "remind") in DB(str(laptop)).sent_events("papaya01")
    from papaya.cycle import run_cycle
    back = Rec()
    assert run_cycle(DB(str(laptop)), "papaya01", [standby._item(r) for r in DB(str(laptop)).upcoming("papaya01", NOW)],
                     back, NOW) == 0 and not back.sent


def test_outage_notice_is_sent_once_per_outage(tmp_path):
    rel = Release(); laptop = tmp_path / "l.db"; laptop_db(laptop)
    rel.put(standby.STATE_ASSET, standby.seal(standby.snapshot(str(laptop)), KEY))
    told = []
    for i in range(3):                             # three runs, one outage (the same last beat)
        standby.run_standby(str(tmp_path / f"w{i}.db"), Rec(), told.append, rel, NOW + timedelta(hours=i),
                            str(int((NOW - timedelta(minutes=180)).timestamp())), key=KEY)
    assert len(told) == 1
    # the laptop came back and went silent again later: a different beat is a new outage
    standby.run_standby(str(tmp_path / "w9.db"), Rec(), told.append, rel, NOW + timedelta(days=1),
                        str(int((NOW + timedelta(hours=20)).timestamp())), key=KEY)
    assert len(told) == 2


def test_no_uploaded_copy_yet_is_harmless(tmp_path):
    out = standby.run_standby(str(tmp_path / "a.db"), Rec(), lambda t: None, Release(), NOW, beat(300), key=KEY)
    assert "no database copy" in out


def test_laptop_without_a_key_does_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPAYA_STATE_KEY", raising=False)
    assert standby.tick(str(tmp_path / "x.db"), Release(), now=1000.0) == []


def test_laptop_throttles_beats_and_uploads(tmp_path):
    laptop = tmp_path / "l.db"; laptop_db(laptop); rel = Release()
    standby._last.update(beat=0.0, push=0.0, pull=0.0)
    t0 = NOW.timestamp()
    assert "beat" in standby.tick(str(laptop), rel, now=t0, key=KEY)
    assert standby.tick(str(laptop), rel, now=t0 + 60, key=KEY) == []                 # too soon
    later = standby.tick(str(laptop), rel, now=t0 + standby.BEAT_EVERY + 1, key=KEY)
    assert later == ["beat"]                                                           # beat only, no re-upload
    assert standby.tick(str(laptop), rel, now=t0 + standby.PUSH_EVERY + 1, key=KEY).count("push") == 1


def test_a_failed_beat_is_retried_soon(tmp_path):
    class Down(Release):
        def set_var(self, n, v): return False
    laptop = tmp_path / "l.db"; laptop_db(laptop)
    standby._last.update(beat=0.0, push=0.0, pull=0.0)
    t0 = NOW.timestamp()
    assert "beat-failed" in standby.tick(str(laptop), Down(), now=t0, key=KEY)
    assert "beat-failed" in standby.tick(str(laptop), Down(), now=t0 + 61, key=KEY)    # not 5 minutes later


def test_a_damaged_download_is_refused(tmp_path):
    rel = Release(); rel.put(standby.STATE_ASSET, b"PPY1" + b"x" * 80)
    with pytest.raises(standby.StateError):
        standby.run_standby(str(tmp_path / "a.db"), Rec(), lambda t: None, rel, NOW, beat(300), key=KEY)
    assert not (tmp_path / "a.db").exists()
