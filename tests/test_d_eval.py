"""Tests of the evaluation script (scoring, metrics, report), without any LLM or database. Owner: D."""

from app.models.agent import AgentAnswer, Claim, ToolCall
from scripts.agent_eval import (
    CASES,
    Case,
    estimate_cost,
    number_cited,
    parse_args,
    rejected_cypher,
    render,
    run_cases,
    score,
    summarise,
)


def test_forty_questions_with_ten_traps():
    assert len(CASES) == 40
    assert len({case.qid for case in CASES}) == 40
    traps = [case for case in CASES if case.expect != "answer"]
    assert len(traps) == 10
    assert {case.expect for case in traps} == {"refuse", "clarify"}


def test_score_answerable_question():
    case = Case("x", "pipeline", "?", names=["SC Alvorada"], numbers=[50_000_000, 0.42], forbid=["CD Mares"],
                nature="predicted")
    good = AgentAnswer(answer="SC Alvorada received €50m.",
                       claims=[Claim(text="42% chance", nature="predicted", evidence_ids=["PREDICTED_MOVE:9->5"])])
    assert score(case, good) == (True, "ok")
    assert score(case, AgentAnswer(answer="Alvorada got 50M")) == (False, "missing: SC Alvorada")
    assert not score(case, AgentAnswer(answer="SC Alvorada got 40M, 42%", claims=good.claims))[0]
    assert "should not" in score(case, AgentAnswer(answer=good.answer + " CD Mares", claims=good.claims))[1]
    assert "no kept claim" in score(case, AgentAnswer(answer=good.answer + " 42%"))[1]
    assert not score(case, AgentAnswer(answer="no", refused=True))[0]
    negative = Case("y", "path", "?", any_of=["no "])
    assert score(negative, AgentAnswer(answer="There is no chain."))[0]
    assert not score(negative, AgentAnswer(answer="Yes, 4 hops."))[0]


def test_score_traps():
    refuse, clarify = Case("t", "trap", "?", expect="refuse"), Case("u", "trap", "?", expect="clarify")
    assert score(refuse, AgentAnswer(answer="No.", refused=True))[0]
    assert not score(refuse, AgentAnswer(answer="Sure!"))[0]
    assert score(clarify, AgentAnswer(answer="Which one?", needs_clarification=True))[0]
    assert not score(clarify, AgentAnswer(answer="Gabriel Rocha"))[0]


def test_number_cited_accepts_percentages():
    assert number_cited(0.42, {42.0}) and number_cited(0.42, {0.42}) and not number_cited(0.42, {4.2})


def test_metrics_report_and_cost():
    trace = [ToolCall(tool="cypher_fallback", succeeded=False, summary="write: DELETE is not allowed"),
             ToolCall(tool="cypher_fallback", succeeded=False, summary="ClientError: boom"),
             ToolCall(tool="value_trend")]
    answers = {
        "Q1?": AgentAnswer(answer="SC Alvorada", trace=trace,
                           claims=[Claim(text="a", nature="verified")],
                           removed_claims=[Claim(text="b", nature="verified")]),
        "Q2?": AgentAnswer(answer="No", refused=True),
    }

    def ask(question):
        if question == "boom?":
            raise RuntimeError("network down")
        return answers[question]

    cases = [Case("q1", "pipeline", "Q1?", names=["SC Alvorada"]), Case("q2", "pipeline", "Q2?"),
             Case("t1", "trap", "boom?", expect="refuse")]
    outcomes = run_cases(ask, cases)
    assert [o.correct for o in outcomes] == [True, False, False]
    assert rejected_cypher(answers["Q1?"]) == 1
    stats = summarise(outcomes)
    assert stats["correct"] == 1 and stats["false_refusals"] == 1 and stats["errors"] == 1
    assert (stats["claims_kept"], stats["claims_removed"], stats["cypher_calls"], stats["cypher_rejected"]) == (
        1, 1, 2, 1)
    usage = {"small-model": {"input_tokens": 1_000_000, "output_tokens": 0},
             "provider/large-model-2026": {"input_tokens": 0, "output_tokens": 2_000_000}}
    tokens_in, tokens_out, cost = estimate_cost(usage, {"small-model": (0.15, 0.6), "large-model": (0.4, 0.9)})
    assert (tokens_in, tokens_out) == (1_000_000, 2_000_000) and abs(cost - 1.95) < 1e-9
    assert estimate_cost(usage, {})[2] is None
    run = {"date": "2026-10-21", "provider": "fake", "small": "s", "large": "l", "tokens_in": 1, "tokens_out": 2,
           "cost_usd": None}
    report = render(outcomes, stats, run)
    assert "| Correct answers (all questions) | 1 / 3 (33 %) |" in report
    assert "n/a (no price given)" in report and "value_trend" in report and "cypher_fallback ✗" in report


def test_arguments():
    args = parse_args(["--limit", "3", "--no-reload", "--price-small", "0.1", "0.2"])
    assert args.limit == 3 and args.no_reload and args.price_small == [0.1, 0.2] and args.price_large is None
