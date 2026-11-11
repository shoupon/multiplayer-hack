PY := .venv/bin/python

.PHONY: install check serve demo viz demo-celebration mcp clean

install:
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e '.[dev]'

check:
	$(PY) -m pytest

serve:
	$(PY) -m fcl serve

demo:
	$(PY) -m demo.dinner

viz:
	$(PY) -m demo.viz

demo-celebration:
	$(PY) -m demo.celebration $(ARGS)

mcp:
	$(PY) -m fcl.mcp_server

clean:
	rm -rf .data .venv .pytest_cache
