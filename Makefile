.PHONY: build test check install uninstall dist

build:
	./build.sh

test:
	PYTHONPATH=. python3 -m pytest

check: build
	python3 -m compileall -q src tests tools
	PYTHONPATH=. python3 -m pytest
	./nvme-doctor --version

install: build
	./nvme-doctor install

uninstall:
	./nvme-doctor remove

dist: build
	python3 -m build
