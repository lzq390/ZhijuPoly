ALTER TABLE generation.polytao_jobs
  ALTER COLUMN progress_message SET DEFAULT 'Waiting for the PolyTAO backend runtime to start.';

ALTER TABLE generation.polytao_jobs
  ALTER COLUMN engine SET DEFAULT 'polytao-backend';
