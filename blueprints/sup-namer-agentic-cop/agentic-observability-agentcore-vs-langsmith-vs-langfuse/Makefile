# Convenience targets for the Agentic Observability blueprint.
#
# The container image is built in AWS CodeBuild at deploy time (deploy-time-build
# stages docker/ as an S3 asset and runs the real `docker build` remotely), so no
# local Docker daemon is required. `validate-image` is a no-Docker pre-flight for
# that build: it runs the hermetic image-build checks (Dockerfile COPY sources
# exist, agent sources compile) and then resolves the pip requirements *without
# installing them*. A syntax error, a missing COPY source, or an unresolvable pin
# therefore fails here instead of minutes into a deploy.
#
# `deploy` declares `validate-image` as a prerequisite, so `make deploy` runs the
# validation first and never starts a doomed CodeBuild if it fails.
#
# The pip resolve needs network access and the resolver from a recent pip; the
# hermetic checks need only pytest (pip install -r requirements-dev.txt).

.PHONY: validate-image deploy

validate-image:
	python3 -m pytest tests/test_image_build.py -q -p no:cacheprovider
	python3 -m pip install --dry-run --ignore-installed -r docker/requirements.txt

deploy: validate-image
	cdk deploy --all
