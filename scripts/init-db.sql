-- ============================================================================
-- PostgreSQL Initialization Script for MEHO
-- ============================================================================
-- This script runs on first container startup to create additional databases.
-- The 'meho' database is created automatically via POSTGRES_DB env var.
-- ============================================================================

-- Create keycloak database for identity management
CREATE DATABASE keycloak;

-- Grant full privileges to the meho user
GRANT ALL PRIVILEGES ON DATABASE keycloak TO meho;

-- Also create the test variant for docker-compose.test.yml
-- This will be used when running tests
CREATE DATABASE keycloak_test;
GRANT ALL PRIVILEGES ON DATABASE keycloak_test TO meho;

