import os
import tempfile

# Must be set before app modules are imported (the DB engine is created at import time).
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="jobhunter-test-"))
os.environ.setdefault("ANTHROPIC_API_KEY", "")
