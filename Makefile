SHELL := /bin/bash

test:
	pytest .

prepare:
	pip install -r requirements.txt
	pip install -r requirements-dev.txt

make_env:
	python -m venv .env 

init_env:
	source .env/bin/activate

lint:
	autopep8 --in-place --aggressive -r .
