# NovaGuard - AI Safety & Governance Toolkit

## Overview

NovaGuard provides comprehensive tools for evaluating, monitoring, and ensuring the safety of AI/ML models in production. It helps organizations meet regulatory requirements, detect bias, and maintain trust in their AI systems.

## Core Capabilities

### 1. Bias Detection & Fairness Auditing

NovaGuard analyzes model predictions across protected attributes (gender, race, age, disability status, etc.) using industry-standard fairness metrics:

- **Demographic Parity**: Ensures prediction rates are similar across groups
- **Equalized Odds**: Ensures true positive and false positive rates are balanced
- **Calibration**: Ensures predicted probabilities match actual outcomes per group
- **Individual Fairness**: Similar individuals receive similar predictions
- **Counterfactual Fairness**: Predictions don't change when protected attributes are flipped

### 2. Explainability

Multiple explanation methods for different stakeholders:

- **SHAP Values**: Feature-level contribution analysis for technical teams
- **LIME**: Local interpretable explanations for individual predictions
- **Counterfactual Explanations**: "What would need to change to get a different outcome?"
- **Natural Language Explanations**: Plain-English summaries for business stakeholders
- **Feature Importance Rankings**: Global model behavior summaries

### 3. Model Evaluation

Comprehensive evaluation beyond accuracy:

- **Robustness Testing**: Adversarial perturbation resistance
- **Out-of-Distribution Detection**: Identify inputs the model wasn't trained on
- **Uncertainty Quantification**: Confidence calibration analysis
- **Slice Analysis**: Performance breakdown by data segments
- **Regression Testing**: Ensure new versions don't degrade on key scenarios

### 4. Compliance Reporting

Pre-built report templates for:

- **EU AI Act**: Risk classification and conformity assessment
- **NIST AI RMF**: Risk management framework alignment
- **IEEE 7010**: Well-being impact assessment
- **Industry-specific**: HIPAA (healthcare), SR 11-7 (banking), FDA (medical devices)

## Integration

### With NovaPlatform

NovaGuard integrates natively with NovaPlatform's model registry:

```python
from novaguard import AuditReport
from novaplatform import ModelRegistry

# Pull model from registry
model = ModelRegistry.get_model("credit-scoring-v2")

# Run comprehensive audit
report = AuditReport(
    model=model,
    dataset="validation_set_q4_2025",
    protected_attributes=["gender", "age_group", "ethnicity"],
    metrics=["demographic_parity", "equalized_odds", "calibration"],
    explanation_methods=["shap", "counterfactual"]
)

report.generate()
report.export_pdf("audit_report_q4_2025.pdf")
```

### Standalone API

```
POST /api/v1/audits
Content-Type: application/json

{
  "model_endpoint": "https://your-model.example.com/predict",
  "dataset_s3_uri": "s3://your-bucket/eval-data.parquet",
  "protected_attributes": ["gender", "age_group"],
  "fairness_metrics": ["demographic_parity", "equalized_odds"],
  "threshold": 0.8
}
```

## Alerting & Monitoring

NovaGuard provides continuous monitoring in production:

- **Fairness Drift**: Alert when fairness metrics deviate from baseline
- **Explanation Shift**: Detect when model reasoning patterns change
- **Compliance Violations**: Automatic flagging of regulatory threshold breaches
- **Scheduled Audits**: Weekly/monthly automated audit reports

## Pricing

| Tier | Models Monitored | Audits/Month | Price |
|------|-----------------|--------------|-------|
| Starter | 5 | 20 | $1,000/mo |
| Professional | 25 | 100 | $4,000/mo |
| Enterprise | Unlimited | Unlimited | Custom |
