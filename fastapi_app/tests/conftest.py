import os
import sys
from pathlib import Path

# main.py exports spans on import; keep a missing collector from slowing the tests.
os.environ.setdefault("OTLP_GRPC_ENDPOINT", "localhost:4317")
os.environ.setdefault("OTLP_INSECURE", "true")
os.environ.setdefault("OTEL_EXPORTER_OTLP_TIMEOUT", "1")

# main.py and utils.py are top-level modules, as in the container image.
sys.path.insert(0, str(Path(__file__).parents[1]))
