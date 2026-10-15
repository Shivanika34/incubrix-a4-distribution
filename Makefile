.PHONY: test openapi

test:
	python -m pytest -q

openapi:
	python -m app.generate_openapi
