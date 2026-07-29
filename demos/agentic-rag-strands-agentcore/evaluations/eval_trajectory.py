"""Trajectory evaluation for the RAG agent.

Evaluates whether the agent uses tools correctly — specifically that it
always retrieves context before answering questions.
"""

from strands import Agent
from strands_evals import Case, Experiment
from strands_evals.evaluators import TrajectoryEvaluator
from strands_evals.extractors import tools_use_extractor

from agent.rag_agent import create_rag_agent
from evaluations.test_cases import TRAJECTORY_EVAL_CASES


def get_response_with_trajectory(case: Case) -> dict:
    """Execute the RAG agent and capture tool usage trajectory."""
    agent = create_rag_agent(enable_tracing=False, callback_handler=None)
    response = agent(case.input)

    # Extract which tools were called
    trajectory = tools_use_extractor.extract_agent_tools_used_from_messages(
        agent.messages
    )

    return {"output": str(response), "trajectory": trajectory}


def run_trajectory_evaluation() -> dict:
    """Run trajectory evaluation.

    Verifies the agent follows the correct tool usage pattern:
    1. Always calls retrieve_from_kb before answering
    2. Doesn't make unnecessary tool calls
    """
    evaluator = TrajectoryEvaluator(
        rubric="""
        Evaluate the agent's tool usage trajectory for a RAG agent:

        Use the `expected_trajectory` to judge correctness. The tool available
        is: retrieve_from_kb.

        **Core principle**: For any question seeking information about NovaTech, the
        agent MUST call `retrieve_from_kb` before answering — it should never answer
        an information-seeking question from memory alone.

        **Important exception**: For inputs that are NOT information requests — such as
        greetings ("Hi there!"), small talk, or meta questions — the CORRECT behavior
        is to NOT call any retrieval tool. An empty `expected_trajectory` means no
        retrieval is expected; calling retrieve_from_kb in that case is the error.

        Scoring:
        - 1.0: Trajectory matches expectation — retrieved when a question needed it
               with a well-formed query, OR correctly skipped retrieval for a
               greeting/non-question. No unnecessary calls.
        - 0.75: Correct retrieval decision but the query could be better optimized,
                or minor inefficiencies.
        - 0.5: Retrieved but with a poor query, or made unnecessary additional calls.
        - 0.0: Wrong retrieval decision — answered an information question from memory
               without retrieving, OR retrieved unnecessarily for a greeting.
        """,
        include_inputs=True,
    )

    # Add tool descriptions for context
    sample_agent = create_rag_agent(enable_tracing=False, callback_handler=None)
    tool_descriptions = tools_use_extractor.extract_tools_description(
        sample_agent, is_short=True
    )
    evaluator.update_trajectory_description(tool_descriptions)

    experiment = Experiment[str, str](
        cases=TRAJECTORY_EVAL_CASES,
        evaluators=[evaluator],
    )

    report = experiment.run_evaluations(get_response_with_trajectory)

    print("\n" + "=" * 60)
    print("TRAJECTORY EVALUATION RESULTS")
    print("=" * 60)
    report.display()

    # Save results
    experiment.to_file("evaluations/results/trajectory_evaluation")
    print("\nResults saved to evaluations/results/trajectory_evaluation.json")

    return {
        "overall_score": report.overall_score,
        "pass_rate": sum(report.test_passes) / len(report.test_passes),
        "num_cases": len(report.cases),
    }


if __name__ == "__main__":
    results = run_trajectory_evaluation()
    print(f"\n\nSummary: Score={results['overall_score']:.2f}, "
          f"Pass Rate={results['pass_rate']:.1%}")
