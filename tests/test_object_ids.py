from __future__ import annotations

from mvp_orbit.core.canonical import object_id_for_json


def test_object_id_is_stable_for_key_order():
    left = {"argv": ["python3", "run.py"], "env_patch": {"A": "1", "B": "2"}, "working_dir": "."}
    right = {"working_dir": ".", "env_patch": {"B": "2", "A": "1"}, "argv": ["python3", "run.py"]}
    assert object_id_for_json(left) == object_id_for_json(right)
