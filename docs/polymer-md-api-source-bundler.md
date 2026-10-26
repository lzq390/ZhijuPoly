# MD API source-bundle developer tool

`scripts/polymer_md_api_services.py` is a standard-library-only input bundler.
It can freeze a reviewed source tree, verify the resulting inventory, or render
a unit file. It does not install dependencies, execute packaged Python, install
a unit, start a service, connect to a database, or run scientific work.

## Input contract

`freeze --source-root PATH --dependency-lock PATH --home PATH` requires all of
the directories in `PACKAGES` and all of the ordinary files in `SCRIPTS`.
The backend development launcher and acceptance-registry script are copied
inputs, not imports of the bundler. A partial checkout that lacks these inputs
is rejected. Supply a complete, separately reviewed input tree; the tool does
not make a partial API implementation operational.

The release home must be outside the source tree. Linked source, lock, release,
or output ancestors are rejected. Release parents must be owned by the caller
and not writable by other users. `render-unit` creates a new mode-0600 output
with exclusive creation and does not overwrite an existing or concurrent file
or symbolic link. Review the generated configuration before any separate,
explicitly authorized installation or execution.

## Verification limits

Non-Python package data must match an explicit path and SHA256 allowlist.
Those pins describe one reviewed MD source snapshot, including its migration
manifest. They deliberately do not accept a later or different manifest. A
source snapshot change requires a separate review and update of the pins.
No scientific fixture or private runtime data is included with this tool.

The constant name `PUBLIC_DATA_FILES` does not confer permission to publish
an input file or its metadata. All source inputs require content and secret
review. The tool excludes hidden files and caches and rejects unknown package
data; it is not a general secret detector for arbitrary Python source.

Dependency input is restricted to nonempty version-pinned records with SHA256
hashes. Comments are omitted, and environment assignments and authenticated
URLs are rejected. This checks record syntax and consistency, not registry
provenance, dependency resolution, package authenticity, or installation.

`verify` checks paths, allowed data, manifest identity and exact file bytes.
It does not establish that API imports resolve, that all transitive runtime
dependencies exist, or that the API is functional. Rendered service paths and
the packaged backend launcher remain caller-reviewed inputs.

## Validation and deferred work

The independent tool group has 22 synthetic tests covering stable inventories,
byte changes, tampering, private-data exclusion, dependency canonicalization,
linked ancestors, release identity, and exclusive unit-file creation. They run
from a directory containing only this source and its tests in an existing
read-only CPU test image with no network and no external process execution.

Four separate backup early-rejection tests are not part of this group. The
backup operator, complete API harness, real source release, real backup and
restore, database roles, unit installation, and scientific execution remain
unverified and are not enabled or claimed by this commit.


Dependency lock input accepts the repository's exact optional leading `--only-binary :all:` directive and standard backslash continuations. The directive and every SHA256 pin are retained; comments are omitted. Other index/control options, unpinned records, embedded backslashes and dangling continuations are rejected. Parsing never installs dependencies.

## Integrated launcher boundary

The checked-in launcher supplies only `preflight-full` and `serve-full` for an existing private development identity. It does not provide the earlier uncommitted initialization, login, submission or password-management prototype commands. Configuration reads reject linked ancestors, hard links, public permissions, oversized files and concurrent replacement. The current data pins include the complete reviewed migration manifest and optional 0024 SQL; the binary and historical SQL pins stay unchanged. Tests use invented configuration and do not run a real service, database migration, backup or scientific job.
