from __future__ import annotations

from mvp_orbit.cli.main import main


def test_keys_generate_prints_task_keypair(capsys):
    try:
        main(["keys", "generate"])
    except SystemExit as exc:
        assert exc.code == 0

    output = capsys.readouterr().out
    assert "ORBIT_TASK_PRIVATE_KEY_B64=" in output
    assert "ORBIT_TASK_PUBLIC_KEY_B64=" in output
