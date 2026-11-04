import base64
import json
import struct
import sys
import time
import zlib


def chunk(kind, data):
    return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack("!2I5B", 1, 1, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(b"\0\x80\x70\xa0"))
    + chunk(b"IEND", b"")
)

print(json.dumps({"ready": True}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if "timeout" in request.get("prompt", ""):
        time.sleep(5)
    if "error" in request.get("prompt", ""):
        print(json.dumps({"error": "simulated OOM"}), flush=True)
    else:
        print(json.dumps({"image": base64.b64encode(PNG).decode()}), flush=True)
