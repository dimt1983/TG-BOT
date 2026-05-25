#!/usr/bin/env python3
"""Compatibility wrapper.

The canonical stock updater is scripts/update_stock_from_xlsx.py.
This file exists only so old Hermes routines do not run the obsolete
RB-BOT.xlsx parser.
"""
from __future__ import annotations

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).with_name("update_stock_from_xlsx.py")), run_name="__main__")
