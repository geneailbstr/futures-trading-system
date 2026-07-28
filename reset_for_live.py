#!/usr/bin/env python3
"""
reset_for_live.py — run ONCE on Monday morning before starting the real eval.
Archives simulation state so the live account starts with clean books.
The real MFFU eval is a fresh account: sim profits, qualifying days, and
consistency history MUST NOT carry over into live rule math.
"""
import os
import shutil
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(HERE, f"sim_archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}")

moved = []
for fname in ("sim_state.json", "state.json"):
    path = os.path.join(HERE, fname)
    if os.path.exists(path):
        os.makedirs(ARCHIVE, exist_ok=True)
        shutil.move(path, os.path.join(ARCHIVE, fname))
        moved.append(fname)

if moved:
    print(f"✅ Archived {', '.join(moved)} -> {ARCHIVE}/")
    print("   Live eval starts with clean state.")
else:
    print("Nothing to archive — state files not found (already reset?)")

print("\nMONDAY CHECKLIST:")
print("  [ ] MFFU Flex $25K eval purchased (codes: IMAN / WIN / SAVE40)")
print("  [ ] Tradovate credentials from MFFU email -> .env")
print("  [ ] Market Data Agreement accepted (Non-Professional)")
print("  [ ] PickMyTrade subscription ACTIVE (had an expiry warning!)")
print("  [ ] PMT connected to Tradovate, new webhook URL -> .env")
print("  [ ] pmt.py rebuilt from PMT generated template (dollar SL/TP)")
print("  [ ] PMT_TEMPLATE_VERIFIED = True in config.py")
print("  [ ] SIMULATION_MODE = False in config.py")
print("  [ ] python3 bot.py -> confirm first signal reaches Tradovate")
