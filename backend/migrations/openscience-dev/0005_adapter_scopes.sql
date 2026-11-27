-- Only the independent development auth cluster carries these grants.
BEGIN;
ALTER TABLE openscience_m3.operations DROP CONSTRAINT operations_tool_check,
    ADD COLUMN channel text NOT NULL DEFAULT 'model' CHECK (channel IN ('model','ui')),
    ADD COLUMN draft_id text,
    ADD COLUMN revision integer,
    ADD CONSTRAINT operations_tool_check CHECK (tool IN (
        'predict_polymer_properties','polymerize_monomers','dft_job_submit','dft_job_list','dft_job_status','dft_job_result',
        'get_polytao_capabilities','calculate_polytao_descriptors','prepare_polytao_generation','get_polytao_draft','get_polytao_job',
        'update_polytao_draft','confirm_polytao_generation','cancel_polytao_draft','get_retrosynthesis_capabilities',
        'resolve_retrosynthesis_target','prepare_monomer_retrosynthesis','get_retrosynthesis_draft','get_retrosynthesis_job',
        'update_retrosynthesis_draft','confirm_monomer_retrosynthesis','cancel_retrosynthesis_draft')),
    ADD CHECK (tool NOT IN ('confirm_polytao_generation','confirm_monomer_retrosynthesis') OR
               (confirmed AND channel='ui' AND draft_id IS NOT NULL AND revision IS NOT NULL AND revision>0));
ALTER TABLE openscience_m3.delegations ADD COLUMN native_key text CHECK (native_key ~ '^[a-f0-9]{64}$');
CREATE INDEX operations_draft ON openscience_m3.operations(owner_id,draft_id) WHERE confirmed;
COMMIT;
