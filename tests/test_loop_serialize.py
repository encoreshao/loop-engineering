import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from loop_budget import BudgetStatus
from loop_result import IterationResult, LoopResult
from loop_serialize import (
    find_latest_result,
    list_results,
    read_result,
    summarize_results,
    to_json_dict,
    write_result,
)
from loop_state import LoopState
from loop_verifiers import VerificationResult


def _sample_result(run_id="run_1", loop_id="loop_1", final_state=LoopState.COMPLETED, cost_usd=0.0):
    verification = VerificationResult(
        name="tests", passed=True, exit_code=0, duration_ms=12, output="ok", evidence={"command": "true"}
    )
    iteration = IterationResult(
        iteration=1,
        state=final_state,
        verification_results=[verification],
        budget={"overall": BudgetStatus.OK, "cost": {"used_usd": cost_usd}},
        progressed=True,
    )
    return LoopResult(
        loop_id=loop_id,
        run_id=run_id,
        definition_name="test-loop",
        final_state=final_state,
        iterations=[iteration],
        stop_reason=final_state.value,
    )


def test_to_json_dict_round_trips_through_json_dumps():
    result = _sample_result()

    data = to_json_dict(result)
    reparsed = json.loads(json.dumps(data))

    assert reparsed["run_id"] == "run_1"
    assert reparsed["final_state"] == "completed"
    assert reparsed["stop_reason"] == "completed"
    assert reparsed["iterations"][0]["state"] == "completed"
    assert reparsed["iterations"][0]["verification_results"][0]["name"] == "tests"
    assert reparsed["iterations"][0]["verification_results"][0]["passed"] is True
    assert reparsed["iterations"][0]["budget"]["overall"] == "ok"


def test_write_result_creates_file_under_results_dir(tmp_path):
    result = _sample_result(run_id="run_abc")

    path = write_result(result, results_dir=tmp_path)

    assert path == tmp_path / "run_abc" / "result.json"
    assert path.exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["run_id"] == "run_abc"


def test_read_result_returns_the_same_data_written(tmp_path):
    result = _sample_result(run_id="run_xyz")
    path = write_result(result, results_dir=tmp_path)

    data = read_result(path)

    assert data["run_id"] == "run_xyz"
    assert data["loop_id"] == "loop_1"


def test_list_results_lists_every_written_run(tmp_path):
    write_result(_sample_result(run_id="run_a"), results_dir=tmp_path)
    write_result(_sample_result(run_id="run_b"), results_dir=tmp_path)

    results = list_results(results_dir=tmp_path)

    assert sorted(p.parent.name for p in results) == ["run_a", "run_b"]


def test_find_latest_result_picks_the_most_recently_written(tmp_path):
    write_result(_sample_result(run_id="run_old"), results_dir=tmp_path)
    older_path = tmp_path / "run_old" / "result.json"
    write_result(_sample_result(run_id="run_new"), results_dir=tmp_path)
    newer_path = tmp_path / "run_new" / "result.json"

    import os
    import time

    time.sleep(0.01)
    os.utime(newer_path, None)

    latest = find_latest_result(results_dir=tmp_path)

    assert latest == newer_path


def test_list_results_empty_dir_returns_empty_list(tmp_path):
    assert list_results(results_dir=tmp_path) == []


def test_find_latest_result_returns_none_when_empty(tmp_path):
    assert find_latest_result(results_dir=tmp_path) is None


def test_summarize_results_empty_dir(tmp_path):
    summary = summarize_results(results_dir=tmp_path)

    assert summary == {
        "total_runs": 0,
        "success_rate": None,
        "escalation_rate": None,
        "average_cost_usd": None,
    }


def test_summarize_results_computes_rates_and_average_cost(tmp_path):
    write_result(_sample_result(run_id="run_a", final_state=LoopState.COMPLETED, cost_usd=1.0), results_dir=tmp_path)
    write_result(_sample_result(run_id="run_b", final_state=LoopState.COMPLETED, cost_usd=3.0), results_dir=tmp_path)
    write_result(_sample_result(run_id="run_c", final_state=LoopState.ESCALATED, cost_usd=2.0), results_dir=tmp_path)
    write_result(_sample_result(run_id="run_d", final_state=LoopState.FAILED, cost_usd=0.0), results_dir=tmp_path)

    summary = summarize_results(results_dir=tmp_path)

    assert summary["total_runs"] == 4
    assert summary["success_rate"] == 0.5
    assert summary["escalation_rate"] == 0.25
    assert summary["average_cost_usd"] == 1.5
