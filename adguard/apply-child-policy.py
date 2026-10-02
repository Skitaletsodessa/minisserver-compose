#!/usr/bin/env python3
"""Applies the children's DNS policy to AdGuard Home. Logic lives in childpolicy.py
(see its docstring); run by adguard-policy.timer every minute. --now ISO for testing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import childpolicy  # noqa: E402

sys.exit(childpolicy.main())
