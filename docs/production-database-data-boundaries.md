# Production database data boundaries

The deployment controller captures one PostgreSQL `REPEATABLE READ`, read-only,
deferrable snapshot after admission is drained and again before admission is
reopened. Every table introduced by migrations 0001–0013 belongs to exactly one
of the following boundaries.

## Business-mutable data

These relations must remain byte-for-byte identical during a code deployment
or migration maintenance:

- `online_knowledge.history`, `online_knowledge.jobs`
- `lab.test_projects`, `lab.sample_measurements`
- `md.monomer_md_jobs`
- after 0013: `monomer_dft.jobs`, `monomer_dft.job_attempts`,
  `monomer_dft.artifacts`

Migration 0013 may only change the three DFT relations from absent to present
and empty. Its `monomer_dft.jobs_enqueue_sequence_seq` must be newly present,
at its start value, and not called.

## Governed controls

- `governance.deployment_control` may change only to a drain owned by the
  current persistent operation ID. Normal deployment uses
  `pull-deploy-controller`; 0012 uses `pull-contract-0012`. A canary may replace
  the initial drain with the exact post-canary drain for the same operation.
- `governance.database_analytics_snapshots` is unchanged on the B path. A
  future release-bound refresh needs a separate, explicit transition contract.

## Static import data

`governance.source_files`, `governance.import_batches`, `core.polymers`,
`core.polymer_properties`, `core.polymer_property_filter_records`,
`knowledge.documents`, `knowledge.formulation_records`, `pi.polymers`,
`pi.tg_predictions`, `pi.monomer_iupac`, `dft.molecule_final`,
`dft.energy_trace`, `experimental.process_records`,
`experimental.property_records`, and `model_registry.assets` are sealed but
never rebuilt by deployment. The release descriptor must keep `datasets=[]`;
asset publication cannot invoke an importer.

The standalone import CLI expands `--dataset all` to static datasets only.
Its retired `online` and `lab` names fail closed, and static `--rebuild`
constructs an exact table list without `CASCADE`. This is an implementation
boundary, not only a deployment-policy convention: no static rebuild can
truncate or overwrite any business-mutable relation.

## Migration ledger and the sole destructive exception

`governance.schema_migrations` must be the exact ordered, checksum-pinned
0001–0011, 0001–0012, or 0001–0013 ledger. The only destructive exception is
`generation.polytao_jobs` during 0012:

1. capture its live row count and schema/content digests under the persistent
   0012 operation ID;
2. create and independently restore-verify the private backup;
3. drop the relation through the exact 0012 checksum;
4. require the captured row count to equal the archived evidence;
5. persist before/after/transition evidence in the operation marker, audit
   manifest, success journal, and current deployment state.

No fixed business-row count is an authority. A failed 0012 must restore every
non-control table, sequence, analytics snapshot, and the PolyTAO exception to
its pre-maintenance state.

## Sequence state

The snapshot also binds every data-bearing serial, identity, and explicit
sequence owned by the classified tables. All sequence state is unchanged
except the pristine 0013 DFT sequence creation described above. This prevents
an apparently unchanged row digest from hiding a future identity collision.
