-- Install with the optional candidate model service.
BEGIN;
DO $$ BEGIN
    IF current_database() <> 'nexpoly_deploy_m6' THEN
        RAISE EXCEPTION 'Expected the private deployment candidate database';
    END IF;
END $$;

ALTER TABLE openscience_m4.model_requests
    DROP CONSTRAINT model_requests_environment_check,
    ADD CONSTRAINT deploy_m6_environment CHECK (environment = 'deploy-m6');
COMMIT;
