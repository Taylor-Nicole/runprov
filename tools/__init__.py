# Copyright (c) 2026 Assistance Publique – Hôpitaux de Paris (AP-HP),
#                    Hôpital Henri-Mondor, and Taylor Nicole Thompson
# SPDX-License-Identifier: BSD-3-Clause
# Licensed under the BSD 3-Clause License — see LICENSE.
"""Instruments pointed AT the package, importable so the suite and `ci.py` share one copy.

Not shipped in the wheel (`pyproject.toml` lists `packages = ["runprov"]`) and deliberately
in the sdist, because a corpus nobody can regenerate is a corpus nobody can trust.
"""
