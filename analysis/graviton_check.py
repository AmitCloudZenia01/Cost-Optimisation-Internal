"""
Graviton compatibility assessment for EC2 instances.

Uses three signal tiers:

Tier 1 — Definitive (no app access needed):
  - Platform = Windows → Blocked
  - AMI Architecture = arm64 → Already ARM, just change instance type
  - x86-only software found in SSM inventory → Blocked

Tier 2 — Strong signal (SSM available):
  - SSM managed, inventory scanned, no blockers found → Safe with caveats
  - arm_verify_software found → Check Required (specific items listed)

Tier 3 — Unknown (SSM not available):
  - AMI x86_64, no SSM → Cannot verify, manual check required
"""


RESULT_ALREADY_GRAVITON = "Already Graviton"
RESULT_SAFE             = "Safe"
RESULT_CHECK_REQUIRED   = "Check Required"
RESULT_BLOCKED          = "Blocked"
RESULT_WINDOWS          = "Blocked (Windows)"
RESULT_UNKNOWN          = "Unknown — Manual Check"


def assess(resource):
    """
    Returns a dict:
      compatible: bool
      result: one of the RESULT_* constants
      risk: Low / Medium / High / N/A
      blockers: list[str]
      caveats: list[str]
      validation_steps: list[str]
    """
    platform       = resource.get("platform", "Linux")
    instance_type  = resource.get("instance_type", "")
    ami_arch       = resource.get("ami_architecture", "unknown")
    ssm_managed    = resource.get("ssm_managed", False)
    x86_blockers   = resource.get("x86_only_software", [])
    verify_list    = resource.get("arm_verify_software", [])
    ssm_app_count  = resource.get("ssm_app_count", 0)

    # ── Already on Graviton ────────────────────────────────────────────────
    graviton_families = {"t4g", "m7g", "m6g", "c7g", "c6g", "r7g", "r6g",
                         "im4gn", "is4gen", "hpc7g", "x2gd"}
    family = instance_type.split(".")[0] if "." in instance_type else ""
    if family in graviton_families:
        return {
            "compatible": True,
            "result": RESULT_ALREADY_GRAVITON,
            "risk": "N/A",
            "blockers": [],
            "caveats": [f"{instance_type} is already a Graviton instance."],
            "validation_steps": [],
        }

    # ── Windows — hard block ───────────────────────────────────────────────
    if "Windows" in platform or "windows" in platform.lower():
        return {
            "compatible": False,
            "result": RESULT_WINDOWS,
            "risk": "N/A",
            "blockers": ["Windows OS — Graviton (ARM64) does not support Windows workloads"],
            "caveats": [],
            "validation_steps": [],
        }

    # ── x86-only software confirmed via SSM ───────────────────────────────
    if x86_blockers:
        return {
            "compatible": False,
            "result": RESULT_BLOCKED,
            "risk": "High",
            "blockers": [f"{s} detected in SSM inventory — no ARM64 support" for s in x86_blockers],
            "caveats": [
                f"SSM inventory ({ssm_app_count} apps scanned) found x86-only software.",
                "These applications have no ARM64 release and cannot run on Graviton.",
            ],
            "validation_steps": [
                "Confirm x86-only software versions with vendor",
                "Check if vendor has announced ARM64 roadmap",
                "Consider containerising with x86 emulation layer as a temporary bridge",
            ],
        }

    # ── AMI is already arm64 — OS migration not needed ────────────────────
    if ami_arch == "arm64":
        if ssm_managed and ssm_app_count > 0 and not verify_list:
            result = RESULT_SAFE
            risk = "Low"
            caveats = [
                f"AMI architecture is arm64 — OS is already ARM-native.",
                f"SSM inventory scanned ({ssm_app_count} apps) — no x86-only software found.",
                "Instance type change only — no OS or application changes needed.",
            ]
            validation = [
                "Change instance type to Graviton equivalent in EC2 Console",
                "Start instance and verify application health",
                "Monitor for 24h — CPU, memory, error rates",
            ]
        else:
            result = RESULT_SAFE
            risk = "Low"
            caveats = [
                "AMI architecture is arm64 — OS is already ARM-native.",
                "SSM not available — application inventory not scanned.",
                "Instance type change should be sufficient, but verify application-level dependencies.",
            ]
            validation = [
                "Install SSM agent to enable deeper inventory scan (recommended)",
                "Change instance type to Graviton equivalent",
                "Run application smoke tests post-change",
            ]
        return {
            "compatible": True, "result": result, "risk": risk,
            "blockers": [], "caveats": caveats, "validation_steps": validation,
        }

    # ── x86_64 AMI — software checks vary by SSM availability ─────────────
    if ssm_managed and ssm_app_count > 0:
        if verify_list:
            return {
                "compatible": None,
                "result": RESULT_CHECK_REQUIRED,
                "risk": "Medium",
                "blockers": [],
                "caveats": [
                    f"AMI is x86_64 — requires new ARM64 AMI and application migration.",
                    f"SSM inventory scanned ({ssm_app_count} apps). No hard blockers, but these need ARM64 version verification:",
                ] + [f"  - {s}" for s in verify_list],
                "validation_steps": [
                    "Verify ARM64 versions of flagged software are available",
                    "Test application stack on a Graviton instance in staging",
                    "Build or pull ARM64-compatible container images",
                    "Update CI/CD pipeline to build multi-arch images (linux/amd64,linux/arm64)",
                    "Plan blue/green cutover with rollback",
                ],
            }
        else:
            return {
                "compatible": True,
                "result": RESULT_SAFE,
                "risk": "Low",
                "blockers": [],
                "caveats": [
                    f"AMI is x86_64 — requires new ARM64 AMI.",
                    f"SSM inventory scanned ({ssm_app_count} apps) — no x86-only software detected.",
                    "All detected software has known ARM64 support.",
                ],
                "validation_steps": [
                    "Launch equivalent Graviton instance with ARM64 AMI",
                    "Deploy application stack and run integration tests",
                    "Update Launch Template/ASG with ARM64 AMI and Graviton instance type",
                    "Drain traffic gradually using blue/green or weighted routing",
                ],
            }
    elif ssm_managed and ssm_app_count == 0:
        return {
            "compatible": None,
            "result": RESULT_CHECK_REQUIRED,
            "risk": "Medium",
            "blockers": [],
            "caveats": [
                "AMI is x86_64 — requires ARM64 AMI migration.",
                "SSM agent present but no application inventory collected yet.",
                "Run SSM inventory collection to enable automated compatibility scan.",
            ],
            "validation_steps": [
                "Enable SSM Inventory in AWS Systems Manager console",
                "Wait for inventory to populate (usually 30 min)",
                "Re-run this report to get automated compatibility assessment",
            ],
        }
    else:
        # No SSM — cannot scan — give manual checklist
        return {
            "compatible": None,
            "result": RESULT_UNKNOWN,
            "risk": "Medium",
            "blockers": [],
            "caveats": [
                "AMI is x86_64 — OS migration to ARM64 AMI required.",
                "SSM agent not installed — application inventory unavailable.",
                "Cannot automatically verify ARM64 compatibility without app access.",
            ],
            "validation_steps": [
                "Install SSM agent for automated inventory scan (recommended)",
                "Manually verify: are all application binaries available as ARM64?",
                "Manually verify: check Docker images for linux/arm64 support",
                "Manually verify: any JNI / native .so libraries must be ARM64-compiled",
                "Manually verify: no x86-only ISV/licensed software (Oracle, SQL Server, etc.)",
                "Test full application stack on Graviton instance in staging before production",
            ],
        }
