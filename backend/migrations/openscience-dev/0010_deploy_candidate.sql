-- Candidate-local workspace and delegated tool records. Legacy clusters keep their original constraints.
BEGIN;
DO $$ BEGIN
    IF current_database() <> 'nexpoly_deploy_m6' THEN
        RAISE EXCEPTION 'Expected the private deployment candidate database';
    END IF;
END $$;

ALTER TABLE openscience_m2.environments
    DROP CONSTRAINT environments_environment_check,
    ADD CONSTRAINT deploy_m6_environment CHECK (environment = 'deploy-m6');
ALTER TABLE openscience_m3.operations
    DROP CONSTRAINT operations_environment_check,
    ADD CONSTRAINT deploy_m6_environment CHECK (environment = 'deploy-m6');
COMMIT;
