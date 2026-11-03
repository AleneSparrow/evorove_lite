.PHONY: run test lint
run:
	.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --ssl-keyfile .local/key.pem --ssl-certfile .local/cert.pem --no-access-log
test:
	.venv/bin/python -m pytest -q
lint:
	.venv/bin/python scripts/lint.py
