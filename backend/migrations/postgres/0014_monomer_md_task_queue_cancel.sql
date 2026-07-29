-- Add durable queue ordering and asynchronous cancellation for formal monomer
-- MD jobs.  The worker assigns queue_sequence only while a job is waiting for
-- the single formal execution slot and clears it when the job is promoted.
ALTER TABLE md.monomer_md_jobs
  ADD COLUMN cancel_requested_at timestamptz,
  ADD COLUMN queue_sequence bigint;

ALTER TABLE md.monomer_md_jobs
  DROP CONSTRAINT monomer_md_jobs_status_check;

ALTER TABLE md.monomer_md_jobs
  ADD CONSTRAINT monomer_md_jobs_status_check
  CHECK (status IN (
    'pending', 'submitted', 'running', 'cancel_requested',
    'completed', 'failed', 'cancelled'
  ));

CREATE SEQUENCE md.monomer_md_queue_sequence_seq;
ALTER SEQUENCE md.monomer_md_queue_sequence_seq
  OWNED BY md.monomer_md_jobs.queue_sequence;

CREATE UNIQUE INDEX uq_monomer_md_jobs_queue_sequence
ON md.monomer_md_jobs(queue_sequence)
WHERE queue_sequence IS NOT NULL;

CREATE INDEX idx_monomer_md_jobs_formal_history
ON md.monomer_md_jobs(created_at DESC, job_id DESC)
WHERE run_mode = 'formal';

CREATE INDEX idx_monomer_md_jobs_formal_active_queue
ON md.monomer_md_jobs(queue_sequence NULLS FIRST, created_at, job_id)
WHERE run_mode = 'formal'
  AND status IN ('pending', 'submitted', 'running', 'cancel_requested');
