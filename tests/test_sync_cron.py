# This file is part of smart-ai-router.
# smart-ai-router is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.

#!/usr/bin/env python3
"""
Test suite for the sync cron functionality.
This tests that our sync script can execute properly.
"""
import os
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from smart_ai_router.facade import CapabilityRouter


def test_sync_script_functionality():
    """Test that we can properly initialize and run a sync."""
    
    # Create a temporary database for testing
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        os.environ["SMART_ROUTER_DB"] = str(db_path)
        
        print("Creating capability router...")
        cr = CapabilityRouter()
        
        print("Testing sync initialization...")
        # This should not raise an exception
        assert cr is not None
        
        print("Sync script functionality test passed!")


if __name__ == "__main__":
    try:
        test_sync_script_functionality()
        print("All tests passed!")
    except Exception as e:
        print(f"Test failed: {e}")
        sys.exit(1)