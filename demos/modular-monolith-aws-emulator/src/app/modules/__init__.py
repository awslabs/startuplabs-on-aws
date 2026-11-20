"""Business modules. Each is a vertical slice: api -> application -> domain -> infrastructure.

Rules (enforced by import-linter, see pyproject.toml):
* a module imports another module only through its `contract` module;
* modules never import `app.platform.adapters`, boto3 or botocore;
* everything a module needs from the platform arrives as a `Platform` facade.

To add a module: create `app/modules/<name>/module.py` exposing `MODULE`, then
append `<name>` to INSTALLED.
"""

INSTALLED = ("identity", "profiles", "jobs")
