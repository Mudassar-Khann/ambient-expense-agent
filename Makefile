.PHONY: install playground run-web lint

# Install project dependencies and CLI tool
install:
	uv pip install -e .
	uv tool install google-agents-cli --force || true

# Start the interactive agents-cli playground UI
playground:
	uv run agents-cli playground

# Run the local FastAPI web service for Pub/Sub triggers on port 8080
run-web:
	uv run python app/fast_api_app.py

# Lint the codebase
lint:
	uv run ruff check .

# Deploy the agent to Agent Runtime
deploy:
	uv run agents-cli deploy --project ambient-expense-agent-501912 --region us-east1
