"""Retrieval relevance evaluation for the RAG agent.

Evaluates whether the knowledge base retrieves relevant context
for the given queries — this is the R in RAG quality.
"""

from strands_evals import Case, Experiment
from strands_evals.evaluators import OutputEvaluator

from agent.rag_agent import create_rag_agent
from evaluations.test_cases import RETRIEVAL_EVAL_CASES


def get_retrieval_response(case: Case) -> str:
    """Execute the RAG agent and return its response for retrieval quality assessment."""
    agent = create_rag_agent(enable_tracing=False, callback_handler=None)
    response = agent(case.input)
    return str(response)


def run_retrieval_evaluation() -> dict:
    """Run retrieval relevance evaluation.

    Assesses whether the agent retrieves and uses the correct source
    documents to answer each question.
    """
    evaluator = OutputEvaluator(
        rubric="""
        Evaluate whether the RAG agent's response demonstrates that RELEVANT
        documents were retrieved from the knowledge base.

        The expected output contains the key facts that should appear if the
        correct documents were retrieved. Check:

        1. **Correct Source Retrieved** (50%): Does the response contain information
           that could only come from the expected source document? The response should
           include specific facts from the expected output.

        2. **Relevance of Retrieved Content** (30%): Is the retrieved content actually
           relevant to the question asked, or did the agent retrieve tangentially
           related content?

        3. **Information Completeness** (20%): Does the response include the key
           facts listed in the expected output, suggesting thorough retrieval?

        Scoring:
        - 1.0: Response clearly uses information from the correct source with all key facts
        - 0.75: Correct source likely retrieved but some key facts missing
        - 0.5: Partially relevant retrieval — some correct info but important gaps
        - 0.25: Mostly irrelevant retrieval — wrong source documents used
        - 0.0: No evidence of relevant retrieval
        """,
        include_inputs=True,
    )

    experiment = Experiment[str, str](
        cases=RETRIEVAL_EVAL_CASES,
        evaluators=[evaluator],
    )

    report = experiment.run_evaluations(get_retrieval_response)

    print("\n" + "=" * 60)
    print("RETRIEVAL RELEVANCE EVALUATION RESULTS")
    print("=" * 60)
    report.display()

    # Save results
    experiment.to_file("evaluations/results/retrieval_evaluation")
    print("\nResults saved to evaluations/results/retrieval_evaluation.json")

    return {
        "overall_score": report.overall_score,
        "pass_rate": sum(report.test_passes) / len(report.test_passes),
        "num_cases": len(report.cases),
    }


if __name__ == "__main__":
    results = run_retrieval_evaluation()
    print(f"\n\nSummary: Score={results['overall_score']:.2f}, "
          f"Pass Rate={results['pass_rate']:.1%}")
