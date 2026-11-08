.PHONY: run test test-browser lint preflight smoke backup-check shellcheck
run:
	.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-proxy-headers --ssl-keyfile .local/key.pem --ssl-certfile .local/cert.pem --no-access-log
test:
	.venv/bin/python -m pytest -q
lint:
	.venv/bin/python scripts/lint.py

test-browser:
	.venv/bin/python -m pytest -q tests/browser -o norecursedirs=

preflight:
	./deploy/preflight.sh

smoke:
	./deploy/smoke.sh "$(BASE_URL)"

backup-check:
	systemd-run --wait --pipe --collect \
		--property=User=evorove --property=Group=evorove \
		--property=EnvironmentFile=/etc/evorove/litestream.env \
		"$(CURDIR)/deploy/backup-check.sh"

shellcheck:
	shellcheck deploy/*.sh
