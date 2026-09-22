"""Verification policy (versioned YAML at VERIFICATION_POLICY_PATH).

The policy names the expected check runs and their trusted workflow for each
selected issue class. A missing file, an unsupported version, or an empty
check list fails closed — verification must never treat "no checks" as
success.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

SUPPORTED_VERSIONS = (1,)


class PolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExpectedCheck:
    name: str
    workflow: str


@dataclass(frozen=True)
class VerificationPolicy:
    version: int
    name: str
    required_checks: tuple[ExpectedCheck, ...]
    trusted_workflows: tuple[str, ...]
    require_head_sha_match: bool
    stale_after_seconds: int
    max_reverify_attempts: int


def load_policy(path: str) -> VerificationPolicy:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        raise PolicyError(f"cannot read verification policy {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyError(f"verification policy {path} is not YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise PolicyError(f"verification policy {path} must be a mapping")
    version = raw.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise PolicyError(
            f"unsupported verification policy version {version!r} in {path}; "
            f"supported: {SUPPORTED_VERSIONS}"
        )

    checks_raw = raw.get("required_checks")
    if not isinstance(checks_raw, list):
        raise PolicyError("verification policy requires a list of required_checks")
    checks: list[ExpectedCheck] = []
    for item in checks_raw:
        if isinstance(item, str):
            checks.append(ExpectedCheck(name=item, workflow=""))
        elif isinstance(item, dict) and item.get("name"):
            checks.append(
                ExpectedCheck(
                    name=str(item["name"]),
                    workflow=str(item.get("workflow", "")),
                )
            )
        else:
            raise PolicyError(f"invalid required_checks entry: {item!r}")
    if not checks:
        # PRD: the policy must not treat an empty check list as success.
        raise PolicyError("verification policy has no required_checks")

    return VerificationPolicy(
        version=int(version),
        name=str(raw.get("name", "verification-policy")),
        required_checks=tuple(checks),
        trusted_workflows=tuple(
            str(w) for w in raw.get("trusted_workflows", [])
        ),
        require_head_sha_match=bool(raw.get("require_head_sha_match", True)),
        stale_after_seconds=int(raw.get("stale_after_seconds", 900)),
        max_reverify_attempts=int(raw.get("max_reverify_attempts", 5)),
    )
