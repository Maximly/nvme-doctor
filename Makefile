.PHONY: test check install uninstall dist

test:
	PYTHONPATH=. python3 -m pytest

check:
	python3 -m compileall -q src tests
	PYTHONPATH=. python3 -m pytest

install:
	./install.sh install

uninstall:
	./install.sh remove

dist:
	python3 -m build
