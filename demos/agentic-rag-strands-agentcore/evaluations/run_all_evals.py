"""Run the complete evaluation suite for the RAG agent.

Executes all evaluators and produces a comprehensive report:
1. Output Quality — factual accuracy and completeness
2. Trajectory — correct tool usage patterns
3. Retrieval Relevance — right documents are retrieved
4. Helpfulness — actionable and user-friendly responses
"""

import os
import json
from datetime import datetime


def main():
    """Run all evaluations and produce a summary report."""
    print("=" * 70)
    print("  AGENTIC RAG - COMPREHENSIVE EVALUATION SUITE")
    print(f"  Timestamp: {datetime.now().isoformat()}")
    print("=" * 70)

    # Ensure results directory exists
    os.makedirs("evaluations/results", exist_ok=True)

    results = {}

    # 1. Output Quality Evaluation
    print("\n\n[1/4] Running Output Quality Evaluation...")
    print("-" * 50)
    try:
        from evaluations.eval_output import run_output_evaluation

        results["output_quality"] = run_output_evaluation()
    except Exception as e:
        print(f"  ERROR: {e}")
        results["output_quality"] = {"error": str(e)}

    # 2. Trajectory Evaluation
    print("\n\n[2/4] Running Trajectory Evaluation...")
    print("-" * 50)
    try:
        from evaluations.eval_trajectory import run_trajectory_evaluation

        results["trajectory"] = run_trajectory_evaluation()
    except Exception as e:
        print(f"  ERROR: {e}")
        results["trajectory"] = {"error": str(e)}

    # 3. Retrieval Relevance Evaluation
    print("\n\n[3/4] Running Retrieval Relevance Evaluation...")
    print("-" * 50)
    try:
        from evaluations.eval_retrieval import run_retrieval_evaluation

        results["retrieval_relevance"] = run_retrieval_evaluation()
    except Exception as e:
        print(f"  ERROR: {e}")
        results["retrieval_relevance"] = {"error": str(e)}

    # 4. Helpfulness Evaluation
    print("\n\n[4/4] Running Helpfulness Evaluation...")
    print("-" * 50)
    try:
        from evaluations.eval_helpfulness import run_helpfulness_evaluation

        results["helpfulness"] = run_helpfulness_evaluation()
    except Exception as e:
        print(f"  ERROR: {e}")
        results["helpfulness"] = {"error": str(e)}

    # --- Summary Report ---
    print("\n\n" + "=" * 70)
    print("  EVALUATION SUMMARY")
    print("=" * 70)
    print(f"\n{'Evaluation':<25} {'Score':<10} {'Pass Rate':<12} {'Cases':<8}")
    print("-" * 55)

    overall_scores = []
    for eval_name, result in results.items():
        if "error" in result:
            print(f"  {eval_name:<23} {'ERROR':<10} {'-':<12} {'-':<8}")
        else:
            score = result.get("overall_score", 0)
            pass_rate = result.get("pass_rate", 0)
            num_cases = result.get("num_cases", 0)
            overall_scores.append(score)
            print(
                f"  {eval_name:<23} {score:<10.2f} {pass_rate:<12.1%} {num_cases:<8}"
            )

    if overall_scores:
        avg_score = sum(overall_scores) / len(overall_scores)
        print("-" * 55)
        print(f"  {'OVERALL':<23} {avg_score:<10.2f}")

    # Save summary
    summary = {
        "timestamp": datetime.now().isoformat(),
        "results": results,
        "overall_average": (
            sum(overall_scores) / len(overall_scores) if overall_scores else 0
        ),
    }

    summary_path = "evaluations/results/summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\n\nFull summary saved to: {summary_path}")
    print("Individual results saved in: evaluations/results/")


if __name__ == "__main__":
    main()
