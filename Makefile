.PHONY: scan test

scan:
	python3 -m src.cloudmover.scanner "$(HOME)/Library/Mobile Documents/com~apple~CloudDocs"

test:
	python3 -m pytest


app:
	./scripts/build_macos_app.sh

open-app:
	open dist/CloudMover.app

clean-app:
	rm -rf build dist
