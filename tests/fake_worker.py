import base64
import json
import sys
import time

print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if "timeout" in request.get("prompt", ""):
        time.sleep(5)
    if "error" in request.get("prompt", ""):
        print(json.dumps({"error": "simulated OOM"}), flush=True)
    else:
        print(json.dumps({"image": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}), flush=True)
