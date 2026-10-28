import httpx2
import pytest
from fastapi.testclient import TestClient
from langchain_typesafe import TypeSafeClassifier

from harness import detector as module
from harness.api import app
from harness.data import sample
from harness.detector import Detector, MODEL, QUESTION, RESERVATION, chat_reply
from harness.report import report, wilson


def transport(p=.8, status=200, tokens=100, observer=None, hazard_values=None):
    def respond(request):
        if observer:
            observer(request)
        answers = {name: {"type": "noul", "noul": v} for name, v in
                   (hazard_values or {"injection": p}).items()}
        return httpx2.Response(status, json={"model": MODEL, "answers": answers,
            "usage": {"input_tokens": tokens, "output_tokens": 0}},
            headers={"x-typesafe-request-id": "test-request"})
    return TypeSafeClassifier(model=MODEL, api_key="test", client=httpx2.Client(
        transport=httpx2.MockTransport(respond)))


def test_wire_cache_and_restart(tmp_path):
    import json
    requests = []
    d = Detector(tmp_path, transport(observer=lambda r: requests.append(json.loads(r.content))))
    assert d.detect("hello")["status"] == "dry_run"
    result = d.detect("hello", execute=True)
    assert result["predicted_label"] == 1
    assert requests == [{"model": MODEL, "state": "hello", "questions": {
        "injection": {"type": "noul", "instructions": QUESTION}}}]
    assert Detector(tmp_path).detect("hello", execute=True)["cached"]
    assert d.totals()["calls"] == 1
    assert d.totals()["accounted_usd"] == pytest.approx(100 * module.PRICE)


@pytest.mark.parametrize("p,decision", [(0, "allow"), (.3, "allow"), (.30001, "review"),
                                       (.69999, "review"), (.7, "block"), (1, "block")])
def test_routing(p, decision):
    assert chat_reply("vpn", {"status": "ok", "injection_probability": p})["decision"] == decision
    assert chat_reply("vpn", {"status": "error", "injection_probability": p})["decision"] == "review"


def test_error_reservation_and_no_retries(tmp_path):
    d = Detector(tmp_path, transport(status=401))
    result = d.detect("hello", execute=True)
    assert result["status"] == "error"
    assert d.totals()["accounted_usd"] == RESERVATION
    assert d.totals()["calls"] == 1
    assert d.detect("hello", execute=True)["cached"]


def test_rate_limit_has_only_one_accounted_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    d = Detector(tmp_path, transport(status=429))
    assert d.detect("hello", execute=True)["status"] == "error"
    assert d.totals()["calls"] == 2
    assert d.totals()["accounted_usd"] == 2 * RESERVATION


def test_budget_and_interrupted_call(tmp_path, monkeypatch):
    d = Detector(tmp_path, transport())
    with d.db() as db:
        db.execute("INSERT INTO calls(key,cost) VALUES (?,?)", (module.cache_key("pending"), .999))
    assert d.detect("pending", execute=True)["status"] == "interrupted"
    assert Detector(tmp_path, transport()).detect("new", execute=True)["status"] == "budget_exhausted"
    monkeypatch.setattr(module, "MAX_CALLS", 1)
    assert d.detect("another", execute=True)["status"] == "budget_exhausted"


def test_oversized_never_calls(tmp_path):
    d = Detector(tmp_path, transport())
    assert d.detect("a" * 32000, execute=True)["status"] == "oversized"
    assert d.totals()["calls"] == 0


def test_sample_and_report(tmp_path):
    rows = [{"id": str(i), "label": i % 2, "length_bin": i % 4, "split": "validation",
             "prompt": str(i), "status": "ok", "injection_probability": float(i % 2),
             "predicted_label": i % 2, "latency_seconds": .1} for i in range(100)]
    assert sample(rows, 20) == sample(rows, 20)
    assert len({r["id"] for r in sample(rows, 20)}) == 20
    summary = report(rows, tmp_path, {"calls": 100, "accounted_usd": .01})
    assert summary["metrics"]["accuracy"] == 1
    assert summary["metrics"]["false_positive_rate"] == 0
    assert summary["metrics"]["brier"] == 0
    assert (tmp_path / "calibration.png").exists()


def test_api_is_dry_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("JEV_EXECUTE", raising=False)
    monkeypatch.setattr("harness.api.Detector", lambda: Detector(tmp_path))
    response = TestClient(app).post("/chat", json={"message": "VPN help"})
    assert response.status_code == 200
    assert response.json()["status"] == "dry_run"
    assert response.json()["decision"] == "review"


def test_zero_observed_errors_still_have_uncertainty():
    assert wilson(0, 10)[1] > .2
    assert wilson(10, 10)[0] < .8


def test_missing_usage_retains_reservation(tmp_path):
    d = Detector(tmp_path, transport(tokens=None))
    assert d.detect("hello", execute=True)["status"] == "ok"
    assert d.totals()["accounted_usd"] == RESERVATION


def test_half_probability_is_positive(tmp_path):
    d = Detector(tmp_path, transport(p=.5))
    assert d.detect("hello", execute=True)["predicted_label"] == 1


def test_concurrent_identical_requests_pay_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    def classify(_):
        return Detector(tmp_path, transport()).detect("same prompt", execute=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(classify, range(2)))
    assert all(r["status"] == "ok" for r in results)
    assert sum(r["cached"] for r in results) == 1
    assert Detector(tmp_path).totals()["calls"] == 1


def test_structured_variant_preserves_prompt_and_has_separate_cache(tmp_path):
    import json
    prompt = 'First task.\nSecond task. "quoted"'
    requests = []
    client = transport(observer=lambda r: requests.append(json.loads(r.content)))
    Detector(tmp_path, client).detect(prompt, execute=True)
    b = Detector(tmp_path, client, variant="B")
    assert b.detect(prompt)["status"] == "dry_run"
    assert b.detect(prompt, execute=True)["status"] == "ok"
    assert requests[1]["state"]["candidate_prompt"] == prompt
    assert requests[0]["questions"] == requests[1]["questions"]
    assert set(requests[1]["state"]) == {"evaluation_context", "candidate_prompt"}
    assert Detector(tmp_path, variant="B").detect(prompt)["cached"]
    assert b.totals()["calls"] == 2


def test_c_request_aggregation_cache_and_report(tmp_path):
    import json
    requests = []
    values = {"instruction_override": .01, "task_substitution": .5, "completion_spoofing": .2}
    client = transport(hazard_values=values, observer=lambda r: requests.append(json.loads(r.content)))
    d = Detector(tmp_path / "state", client, variant="C")
    result = d.detect("Original prompt", execute=True)
    assert result["predicted_label"] == 1
    assert result["routing_score"] == .5
    assert result["injection_probability"] is None
    assert result["hazard_probabilities"] == values
    assert requests[0]["questions"] == module.C_CONFIG["questions"]
    assert requests[0]["state"]["candidate_prompt"] == "Original prompt"
    assert Detector(tmp_path / "state", variant="C").detect("Original prompt")["cached"]
    assert Detector(tmp_path / "state", variant="B").cached("Original prompt") is None
    row = {"id": "test:0", "label": 1, "length_bin": 0, "split": "test", "prompt": "Original prompt", **result}
    summary = report([row], tmp_path / "report", d.totals())
    assert "brier" not in summary["metrics"]
    assert "brier" not in summary["intervals_95"]
    assert not (tmp_path / "report/calibration.png").exists()
    assert "not a calibrated injection probability" in (tmp_path / "report/report.md").read_text()


def test_stage_cost_ceiling(tmp_path):
    d = Detector(tmp_path, transport())
    assert d.detect("hello", execute=True, max_project_cost=.001)["status"] == "budget_exhausted"
    assert d.totals()["calls"] == 0
