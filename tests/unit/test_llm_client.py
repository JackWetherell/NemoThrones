import json

import pytest
from pydantic import BaseModel

from nemothrones.config import Settings
from nemothrones.llm import LLM, LLMUnavailable
from nemothrones.llm.backends import BackendError, MockBackend
from tests.conftest import run


class Out(BaseModel):
    answer: int


class Failing:
    name = "failing"

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, **kw):
        self.calls += 1
        raise BackendError("down")

    async def aclose(self):
        pass


def make(tmp_path, backends, chain):
    s = Settings(home=tmp_path)
    for role in s.llm.roles.values():
        role.backend, role.fallback = chain[0], chain[1:]
    return LLM(s, backends=backends)


def test_repair_retry(tmp_path):
    replies = iter(["not json at all", json.dumps({"answer": 7})])
    mock = MockBackend(lambda role, msgs: next(replies), name="m")
    llm = make(tmp_path, {"m": mock}, ["m"])
    out = run(llm.call("gm", "sys", "user", Out))
    assert out.answer == 7
    assert len(mock.calls) == 2
    assert "not valid" in mock.calls[1][1][-1]["content"]


def test_fallback_and_circuit_breaker(tmp_path):
    bad = Failing()
    good = MockBackend(lambda role, msgs: '{"answer": 1}', name="good")
    llm = make(tmp_path, {"failing": bad, "good": good}, ["failing", "good"])
    for _ in range(4):
        assert run(llm.call("gm", "s", "u", Out)).answer == 1
    assert bad.calls == 2  # breaker opened after two failures
    assert llm.last_backend["gm"] == "good"
    assert any("failing" in n for n in llm.notices)


def test_all_backends_fail(tmp_path):
    llm = make(tmp_path, {"failing": Failing()}, ["failing"])
    with pytest.raises(LLMUnavailable):
        run(llm.call("gm", "s", "u", Out))


def test_log_sink_and_cost(tmp_path):
    rows = []
    llm = make(tmp_path, {"m": MockBackend(lambda r, m: '{"answer": 2}', name="m")}, ["m"])
    llm.log_sink = rows.append
    run(llm.call("agent", "sys", "hello", Out, agent_id="x"))
    assert rows and rows[0]["ok"] and rows[0]["agent_id"] == "x" and rows[0]["cost"] > 0
    assert llm.budget.spent > 0
