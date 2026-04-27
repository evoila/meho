#!/bin/bash
# Run only unit tests (fast, no external dependencies)
pytest -m unit --no-cov "$@"

