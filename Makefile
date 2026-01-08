.PHONY: install run run-tui clean help

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Install dependencies
	uv sync

run:  ## Run web demo (http://localhost:8000)
	uv run python server.py

run-tui:  ## Run terminal TUI demo
	uv run --with textual python tui_example.py

clean:  ## Remove cache and build artifacts
	rm -rf __pycache__ .venv *.egg-info dist build .ruff_cache .mypy_cache

lint:  ## Run linter
	uv run --with ruff ruff check .

format:  ## Format code
	uv run --with ruff ruff format .
