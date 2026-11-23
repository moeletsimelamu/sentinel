-- Runs once, on first initialisation of the Postgres volume.
-- A separate database for the automated test suite, so tests never touch dev data.
CREATE DATABASE sentinel_test OWNER sentinel;
