# Opt-in MD Backend configuration

These overlays configure the existing development Backend only. They do not start
a Host Worker, initialize a database, generate credentials or enable submission
by default. The ordinary user-isolation development overlays and a separately
prepared private Host Worker/socket/job root remain prerequisites.

`docker-compose.polymer-md-dev.yml` requires explicit private socket/job paths
and start-authorization input. Both host binds are read only and refuse automatic
host-directory creation. Preflight keeps the exact polymer MD 0023 schema and
service-context checks. Submission defaults to false.

The separate registry overlay requires an explicit existing registry path and
its UID/GID, mounts the registry read only with `create_host_path: false`, and
does not turn on MD or submission. It belongs only in a reviewed drained setup.
No registry contents or private values are versioned here.

Validation renders Compose configuration with synthetic environment inputs;
it contacts no Docker daemon and starts or stops no services. Actual deployment,
database, authorization-token and scientific acceptance are not established by
these configuration tests.
