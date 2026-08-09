.PHONY: build test check install uninstall dist

build:
	python3 tools/build_single.py nvme-doctor

test:
	PYTHONPATH=. python3 -m pytest

check: build
	python3 -m compileall -q src tests tools
	PYTHONPATH=. python3 -m pytest
	./nvme-doctor --version

install:
	./install.sh install

uninstall:
	./install.sh remove

dist: build
	python3 -m build
