#!/bin/bash
# Watch mode for development - reruns tests on file changes
echo "Starting pytest in watch mode..."
echo "Only running unit tests (fast feedback)"
pytest-watch -m unit --no-cov

