"""Live, on-demand evaluation of a single agent response.

Used by the UI to score a response in real time with Strands Evals'
LLM-as-a-judge evaluators. This mirrors the offline evaluation suite
(evaluations/) but applied to a single live interaction, covering four
dimensions:

  1. Output Quality (groundedness + relevance + clarity)
  2. Helpfulness (Strands' built-in 7-level evaluator)
  3. Retrieval Relevance (are the retrieved passages relevant to the question)
  4. Trajectory (did the agent use its tools correctly)
"""

from strands_evals.evaluators import (
    OutputEvaluator,
    TrajectoryEvaluator,
)
from strands_evals.types.evaluation import EvaluationData


# --- Rubrics -----------------------------------------------------------------

_GROUNDED_RUBRIC = """
Evaluate the RAG assistant's response to the user's question. The retrieved
knowledge base context that the assistant was given is included in the input.
Judge groundedness AGAINST THAT CONTEXT.

1. **Groundedness** (40%): Are the response's specific claims (steps, paths,
   commands, values, contacts) supported by the retrieved context provided?
   Facts present in the context are correct and grounded — do NOT treat them as
   fabricated. Only penalize claims that contradict or are absent from the context.
   Reward the response for acknowledging when something is not in the context.

2. **Relevance & Completeness** (35%): Does it directly and fully answer the
   question, making good use of the retrieved context?

3. **Clarity** (25%): Is it well-structured, professional, and easy to read?

Scoring:
- 1.0: Excellent — fully grounded in context, directly answers, clear
- 0.75: Good — minor gaps
- 0.5: Acceptable — partial answer or minor unsupported detail
- 0.25: Poor — largely unhelpful or several unsupported claims
- 0.0: Failing — wrong, contradicts context, or off-topic
"""

_NO_CONTEXT_RUBRIC = """
Evaluate the RAG assistant's response to the user's question. No retrieved
context is available, so judge on intrinsic quality: groundedness (40%),
relevance & completeness (35%), and clarity (25%). Do not assume specific
details are fabricated unless obviously implausible. Score 0.0-1.0.
"""

_RETRIEVAL_RUBRIC = """
Evaluate the RELEVANCE of the retrieved knowledge base passages (provided as the
output) to the user's question (provided as the input). This measures retrieval
quality — the "R" in RAG — independent of the final answer.

1. **Topical Relevance** (60%): Do the retrieved passages actually address the
   subject of the question? Would they contain the answer?
2. **Coverage** (40%): Do the passages appear to include the specific
   information needed to answer the question fully?

Scoring:
- 1.0: Highly relevant passages that clearly contain the answer
- 0.75: Relevant, most needed info present
- 0.5: Partially relevant, some useful content but gaps
- 0.25: Mostly off-topic passages
- 0.0: Irrelevant or empty retrieval
"""

_HELPFULNESS_RUBRIC = """
Rate how HELPFUL the assistant's response is to the user, using a 7-level scale
adapted from the Strands helpfulness evaluator. Consider whether it addresses
the user's actual need, provides actionable next steps, is appropriately
detailed, and anticipates follow-ups.

Map the 7 levels to a 0-1 score:
- 1.0  (Exceptional): Fully resolves the need with clear, actionable guidance
       and useful anticipation of next steps.
- 0.83 (Very helpful): Addresses the need well with actionable detail.
- 0.66 (Helpful): Answers the core need but could be more complete or actionable.
- 0.5  (Moderately helpful): Partially helpful; notable gaps.
- 0.33 (Slightly helpful): Touches the topic but largely unactionable.
- 0.16 (Minimally helpful): Barely relevant or mostly generic.
- 0.0  (Not helpful): Fails to address the need or is off-topic.

test_pass is true when the score is >= 0.66.
"""

_TRAJECTORY_RUBRIC = """
Evaluate the agent's tool-use trajectory. Available tools: retrieve_from_kb,
summarize_context.

Core principle: for an information-seeking question about NovaTech, the agent
MUST call retrieve_from_kb before answering (never answer from memory). For
greetings or non-questions, calling no tool is correct.

Scoring:
- 1.0: Correct tool decision — retrieved for an info question with a sensible
       query, or correctly skipped retrieval for a greeting. No wasteful calls.
- 0.75: Correct decision, minor inefficiency.
- 0.5: Retrieved but with a weak query, or an unnecessary extra call.
- 0.0: Wrong decision — answered an info question without retrieving, or
       retrieved unnecessarily.
"""


def _run(evaluator, data) -> dict:
    """Run one evaluator and normalize its first result to a dict."""
    results = evaluator.evaluate(data)
    if not results:
        return {"error": "No result"}
    r = results[0]
    return {
        "score": float(r.score),
        "test_pass": bool(r.test_pass),
        "reason": r.reason or "",
        "label": r.label,
    }


def evaluate_output_quality(question: str, answer: str, retrieved_context: str = "") -> dict:
    """Groundedness + relevance + clarity, judged against retrieved context."""
    try:
        if retrieved_context.strip():
            rubric = _GROUNDED_RUBRIC
            judge_input = (
                f"## User Question\n{question}\n\n"
                f"## Retrieved Knowledge Base Context (ground truth)\n"
                f"{retrieved_context}"
            )
        else:
            rubric = _NO_CONTEXT_RUBRIC
            judge_input = f"## User Question\n{question}"
        evaluator = OutputEvaluator(rubric=rubric, include_inputs=True)
        return _run(evaluator, EvaluationData(
            input=judge_input, actual_output=answer, name="live-output"))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def evaluate_helpfulness(question: str, answer: str) -> dict:
    """Helpfulness via a 7-level rubric (LLM judge).

    Note: Strands' native HelpfulnessEvaluator is trace-level and needs an OTel
    Session. For single live responses we apply the equivalent 7-level scale as
    an OutputEvaluator rubric, which gives the same signal without per-response
    trace capture. The offline suite still uses the native trace-based evaluator.
    """
    try:
        evaluator = OutputEvaluator(rubric=_HELPFULNESS_RUBRIC, include_inputs=True)
        return _run(evaluator, EvaluationData(
            input=question, actual_output=answer, name="live-helpfulness"))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def evaluate_retrieval(question: str, retrieved_context: str) -> dict:
    """Relevance of the retrieved passages to the question."""
    if not retrieved_context.strip():
        return {"error": "No retrieval performed for this response."}
    try:
        evaluator = OutputEvaluator(rubric=_RETRIEVAL_RUBRIC, include_inputs=True)
        # The "output" being judged here is the retrieved context itself.
        return _run(evaluator, EvaluationData(
            input=question, actual_output=retrieved_context, name="live-retrieval"))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def evaluate_trajectory(question: str, answer: str, trajectory: list) -> dict:
    """Whether the agent used its tools correctly."""
    try:
        evaluator = TrajectoryEvaluator(rubric=_TRAJECTORY_RUBRIC, include_inputs=True)
        return _run(evaluator, EvaluationData(
            input=question,
            actual_output=answer,
            actual_trajectory=trajectory or [],
            name="live-trajectory",
        ))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def evaluate_all(question: str, answer: str, retrieved_context: str = "",
                 trajectory: list | None = None) -> dict:
    """Run all four evaluators concurrently and return results by dimension.

    Each evaluator is a blocking Bedrock (LLM judge) call, so they are run in a
    thread pool to overlap the network latency — roughly 4x faster wall-clock
    than running them sequentially.

    Returns:
        dict mapping dimension name -> result dict (score/test_pass/reason/label
        or error). Also includes 'overall' = mean of available numeric scores.
    """
    from concurrent.futures import ThreadPoolExecutor

    trajectory = trajectory or []

    # Map each dimension to its zero-arg callable
    tasks = {
        "Output Quality": lambda: evaluate_output_quality(
            question, answer, retrieved_context),
        "Helpfulness": lambda: evaluate_helpfulness(question, answer),
        "Retrieval Relevance": lambda: evaluate_retrieval(question, retrieved_context),
        "Trajectory": lambda: evaluate_trajectory(question, answer, trajectory),
    }

    results: dict = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn): dim for dim, fn in tasks.items()}
        for future in futures:
            dim = futures[future]
            try:
                results[dim] = future.result()
            except Exception as e:  # noqa: BLE001
                results[dim] = {"error": str(e)}

    scores = [r["score"] for r in results.values() if "score" in r]
    results["overall"] = sum(scores) / len(scores) if scores else None
    return results


# Backwards-compatible single-dimension entry point (used previously by the UI).
def evaluate_response(question: str, answer: str, retrieved_context: str = "") -> dict:
    """Deprecated: use evaluate_all. Kept for compatibility — returns output quality."""
    return evaluate_output_quality(question, answer, retrieved_context)
