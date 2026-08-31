.PHONY: scan test

scan:
	python3 -m src.cloudmover.scanner "$(HOME)/Library/Mobile Documents/com~apple~CloudDocs"

test:
	python3 -m pytest
