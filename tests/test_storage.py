import json

import pytest

from ftuni.storage import CorruptedError, Store


def test_mirror_detects_and_repairs(tmp_path):
    s = Store([tmp_path / "d1", tmp_path / "d2"], checksums=True)
    doc = {"student_id": "S1", "gpa": 3.45}
    s.write("S1", doc)
    p = tmp_path / "d1" / "S1.json"
    p.write_bytes(p.read_bytes().replace(b"3.45", b"1.45"))     # silent corruption
    got, repaired = s.read("S1")
    assert got == doc and repaired == ["d1"]
    assert json.loads(p.read_bytes()) == doc                    # fixed on disk too


def test_missing_copy_is_rebuilt(tmp_path):
    s = Store([tmp_path / "d1", tmp_path / "d2"], checksums=True)
    s.write("S2", {"a": 1})
    (tmp_path / "d1" / "S2.json").unlink()
    assert s.read("S2") == ({"a": 1}, ["d1"])


def test_both_copies_bad(tmp_path):
    s = Store([tmp_path / "d1", tmp_path / "d2"], checksums=True)
    s.write("S3", {"a": 1})
    for d in ("d1", "d2"):
        (tmp_path / d / "S3.json").write_bytes(b'{"a": 2}')
    with pytest.raises(CorruptedError):
        s.read("S3")


def test_baseline_serves_corrupted_data(tmp_path):
    s = Store([tmp_path / "d1"], checksums=False)
    s.write("S4", {"gpa": 3.45})
    p = tmp_path / "d1" / "S4.json"
    p.write_bytes(p.read_bytes().replace(b"3.45", b"1.45"))
    assert s.read("S4") == ({"gpa": 1.45}, [])                 # nobody notices


def test_scrub_fixes_second_disk(tmp_path):
    s = Store([tmp_path / "d1", tmp_path / "d2"], checksums=True)
    s.write("S5", {"a": 1})
    (tmp_path / "d2" / "S5.json").write_bytes(b"garbage")
    assert s.scrub()["repaired"] == 1
    assert json.loads((tmp_path / "d2" / "S5.json").read_bytes()) == {"a": 1}
