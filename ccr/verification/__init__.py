"""Verification command detection and execution."""

from ccr.verification.commands import detect_verification_commands
from ccr.verification.runner import CommandResult, VerificationReport, run_commands

__all__ = [
    "CommandResult",
    "VerificationReport",
    "detect_verification_commands",
    "run_commands",
]
