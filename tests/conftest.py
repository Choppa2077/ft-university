import os
import tempfile

# must happen before ftuni.config is imported anywhere
os.environ["FTUNI_RUN_DIR"] = tempfile.mkdtemp(prefix="ftuni-test-")
os.environ["FTUNI_MODE"] = "baseline"
