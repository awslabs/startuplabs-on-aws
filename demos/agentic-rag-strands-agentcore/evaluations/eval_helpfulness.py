"""Helpfulness evaluation for the RAG agent.

Uses Strands Evals' HelpfulnessEvaluator with seven-level scoring
to assess how useful and actionable the agent's responses are.
"""

from strands import Agent
from strands_evals import eval_task, TracedHandler, Case, Experiment
from strands_evals.evaluators import HelpfulnessEvaluator

from agent.rag_agent import create_rag_agent
from evaluations.test_cases import HELPFULNESS_EVAL_CASES


@eval_task(TracedHandler())
def rag_helpfulness_task():
    """Create the RAG agent for helpfulness evaluation with tracing."""
    return create_rag_agent(enable_tracing=True, callback_handler=None)


def run_helpfulness_evaluation() -> dict:
    """Run helpfulness evaluation.

    Uses seven-level scoring to assess:
    - How actionable the response is
    - Whether it provides clear next steps
    - Whether it addresses the user's underlying need
    - How well it explains complex topics
    """
    evaluator = HelpfulnessEvaluator()

    experiment = Experiment[str, str](
        cases=HELPFULNESS_EVAL_CASES,
        evaluators=[evaluator],
    )

    report = experiment.run_evaluations(rag_helpfulness_task)

    print("\n" + "=" * 60)
    print("HELPFULNESS EVALUATION RESULTS")
    print("=" * 60)
    report.display()

    # Save results
    experiment.to_file("evaluations/results/helpfulness_evaluation")
    print("\nResults saved to evaluations/results/helpfulness_evaluation.json")

    return {
        "overall_score": report.overall_score,
        "pass_rate": sum(report.test_passes) / len(report.test_passes),
        "num_cases": len(report.cases),
    }


if __name__ == "__main__":
    results = run_helpfulness_evaluation()
    print(f"\n\nSummary: Score={results['overall_score']:.2f}, "
          f"Pass Rate={results['pass_rate']:.1%}")
