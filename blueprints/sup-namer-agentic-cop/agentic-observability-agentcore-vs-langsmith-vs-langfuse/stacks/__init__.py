"""CDK stacks and constructs for the Agentic Observability & Evaluations sample.

Modules in this package are added incrementally by the implementation tasks:

  * app_config             - input resolution + validation (fail-fast)
  * network_stack          - dedicated VPC for the cluster
  * cluster_stack          - EKS cluster, node group, OIDC/IRSA, add-ons
  * observability_stack    - AgentCore log group + Transaction Search
  * agent_image            - Docker image build + dedicated named ECR repo
  * workload_stack         - image asset, scoped IAM, secrets, Deployment/Service
  * observability_config   - the 3-platform env-var map (teaching artifact)
"""
