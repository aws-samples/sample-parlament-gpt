"""Shared library for the parliamentary-debate fetcher Lambdas.

One Lambda per government fetches debate/speech data from that government's official
source, normalizes it into the common :class:`~gov_debates.contracts.SpeechResult`
envelope, and returns it to the AgentCore Gateway. This package holds everything those
Lambdas share:

* ``contracts``  — the normalized result schema (the cross-layer contract).
* ``http``       — a host-pinned HTTP client (per-Lambda egress control) + pagination.
* ``normalize``  — date and text normalizers for wildly inconsistent upstream formats.
* ``gateway``    — the AgentCore Gateway Lambda dispatch/handler contract.
* ``secrets``    — Secrets Manager lookup, cached per ARN.

Design note: egress control is per-Lambda. Each function is constructed with its OWN
host allowlist (never a caller-supplied one), so a compromise of one fetcher can reach
only that jurisdiction's hosts — see :mod:`gov_debates.http.pinned_client`.
"""
from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
