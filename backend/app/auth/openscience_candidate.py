"""Fixed loopback candidate profiles used by the independent OpenScience authorities."""

import json
import os
from pathlib import Path
import re
from uuid import UUID


PROFILES = json.loads(Path(__file__).with_name("openscience_candidates.json").read_bytes())


def authority_environment():
    name = os.environ.get("OPENSCIENCE_M2_ENVIRONMENT", "dev-m1")
    if name not in PROFILES:
        raise ValueError("Unknown independent OpenScience development environment")
    return name


def candidate_database(environment):
    if environment not in PROFILES:
        raise ValueError("Unknown OpenScience candidate")
    return "nexpoly_deploy_m6" if environment == "deploy-m6" else "nexpoly_m2_dev"


def validate_candidate_dsn(environment, dsn, role):
    from psycopg.conninfo import conninfo_to_dict

    options = conninfo_to_dict(dsn)
    if options.get("dbname") != candidate_database(environment):
        raise ValueError("OpenScience authority requires its dedicated candidate database")
    if environment == "deploy-m6" and (
        options.get("host") != "/m2pg"
        or options.get("hostaddr")
        or options.get("service")
        or options.get("user") != f"m2_{role}"
    ):
        raise ValueError("Use the candidate's private PostgreSQL socket and scoped role")


def candidate_members(environment, path=None):
    """Load the administrator's two bindings. Order selects the private A/B seed credentials."""
    if environment not in PROFILES:
        raise ValueError("Unknown OpenScience candidate")
    if environment != "deploy-m6":
        if path is not None or "OPENSCIENCE_CANDIDATE_MEMBERS_FILE" in os.environ:
            raise ValueError("A deployment member registry requires the deploy-m6 environment")
        return [
            {
                "user_id": f"10000000-0000-4000-8000-{number:012d}",
                "username": f"test-user-{label}",
                "workspace_user_id": f"test-user-{label}",
            }
            for number, label in enumerate("ab", 1)
        ]
    path = path or os.environ.get("OPENSCIENCE_CANDIDATE_MEMBERS_FILE")
    if not path:
        raise ValueError("The deployment candidate requires an explicit member registry")
    registry = json.loads(Path(path).read_bytes())
    if (
        not isinstance(registry, dict)
        or set(registry) != {"environment", "members"}
        or registry["environment"] != environment
        or not isinstance(registry["members"], list)
        or len(registry["members"]) != 2
    ):
        raise ValueError("Expected two owner bindings for the selected candidate")
    fields = {"user_id", "username", "workspace_user_id"}
    for m in registry["members"]:
        if (
            not isinstance(m, dict)
            or set(m) != fields
            or not all(isinstance(v, str) for v in m.values())
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", m["username"])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", m["workspace_user_id"])
        ):
            raise ValueError("Invalid candidate member")
        user_id = UUID(m["user_id"])
        if str(user_id) != m["user_id"] or user_id.int <= 1:
            raise ValueError("Expected a non-system canonical platform UUID")
    if any(len({m[k] for m in registry["members"]}) != 2 for k in fields):
        raise ValueError("Candidate owners, logins and workspace bindings must be distinct")
    return registry["members"]
