export PYTHONPATH := packages

.PHONY: test hygiene doctor mock init allocate export-csv status install

test:
	python3 -m pytest

hygiene:
	python3 scripts/public-hygiene-gate.py --repo-root .

doctor:
	python3 -m gsuite_seatpool.cli doctor

mock:
	python3 -m gsuite_seatpool.cli mock

init:
	python3 -m gsuite_seatpool.cli init-tenant --fqdn alpha.example.test --fqdn beta.example.test --seats 50

export-csv:
	python3 -m gsuite_seatpool.cli export-csv --out-dir ./csv_out

allocate:
	python3 -m gsuite_seatpool.cli allocate --target github --count $${N:-4} --max-per-domain-day $${CAP:-2}

status:
	python3 -m gsuite_seatpool.cli status

install:
	python3 -m pip install -e ".[dev]"
