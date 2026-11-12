.PHONY: install lint test e2e

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
	terraform -chdir=infra fmt -check -recursive

test:
	.venv/bin/python -m pytest -v -W ignore::DeprecationWarning

# Needs aws-emulator and terraform on PATH. See README.
e2e:
	PYTHON=.venv/bin/python ./scripts/e2e.sh
