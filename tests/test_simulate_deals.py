import json
import subprocess
import sys


def test_simulate_deals_cli_smoke_defaults_to_all_player_modes(tmp_path):
    report_path = tmp_path / "deal-simulation.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mendicot.tools.simulate_deals",
            "--deals",
            "1",
            "--json-output",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "MendiCot secure deal simulation" in result.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["player_modes"] == [4, 6, 8]
    assert report["total_deals_requested"] == 3
    assert report["total_deals_completed"] == 3

    for player_count in (4, 6, 8):
        mode = report["modes"][str(player_count)]
        assert mode["hand_size"] == 48 // player_count
        assert mode["commitment_verification_failures"] == 0
        assert not any(mode["invariant_failures"].values())
        assert len(mode["card_position_frequency"]) == 48
        assert all(
            len(position_counts) == 48
            for position_counts in mode["card_position_frequency"].values()
        )
        assert set(mode["seat_card_frequency"]) == {
            str(seat) for seat in range(player_count)
        }
        assert sum(mode["mendis_per_hand"].values()) == player_count
        assert all(
            sum(histogram.values()) == 1
            for histogram in mode["mendis_per_team"].values()
        )
        assert mode["timing_ms"]["total"]["samples"] == 1
