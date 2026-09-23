from dataclasses import replace
import json
import random

import pytest

from jevgame import audit, benchmark
from jevgame.episode import EpisodeResult
from jevgame.physics import random_initial_state


def test_benchmark_reproducible_starts_and_deduplicated_usage(tmp_path, monkeypatch):
    starts = {}
    def fake_episode(*, db_path, start_state, **kwargs):
        assert "variants" not in kwargs and "use_triple_loop" not in kwargs
        starts[start_state.terrain_seed] = start_state
        final = replace(start_state, status="landed", x=0, y=0, vx=0, vy=-1, angle_deg=0, tick=1)
        conn = audit.connect(db_path)
        for step in ("attitude", "throttle"):
            audit.log_tick(conn, episode_id=db_path.stem, tick=0, question={},
                           raw_answer={"model": "fixture", "usage": {"input_tokens": 100}},
                           confidence=.9, threshold=.55, outcome="applied", action_applied="no_op",
                           state_before=start_state.as_dict(), state_after=final.as_dict() if step == "throttle" else None,
                           step=step, variant="D")
        conn.close()
        return EpisodeResult(db_path.stem, final, 1, {"applied": 2})
    monkeypatch.setattr(benchmark, "run_episode", fake_episode)
    output = tmp_path / "benchmark"
    summary = benchmark.run_benchmark(output=output, seeds=[12, 13])
    assert set(summary) == {"D"}
    assert summary["D"]["input_tokens"] == 200
    for seed in (12, 13):
        expected = random_initial_state(random.Random(seed))
        assert starts[expected.terrain_seed] == expected
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["seeds"] == [12, 13]
    assert "guidance.py" in manifest["source_sha256"]
    assert len((output / "episodes.jsonl").read_text().splitlines()) == 2
    with pytest.raises(FileExistsError):
        benchmark.run_benchmark(output=output, seeds=[12])
