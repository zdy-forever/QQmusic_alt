#!/usr/bin/env python3
"""Build the offline single-file version of 月下方海.

Takes the published page (moon-cube-sea.html), wraps it in a full HTML document and inlines three.js r128,
so the result opens by double-click with no network. Fonts fall back to the system's serif and monospace
faces when offline.
"""
import sys
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "moon-cube-sea.html")
THREE = Path(sys.argv[2])
OUT = Path(sys.argv[3] if len(sys.argv) > 3 else "月下方海-离线版.html")
CDN = '<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>'

page = SRC.read_text(encoding="utf-8")
lib = THREE.read_text(encoding="utf-8")
assert page.count(CDN) == 1, "three.js script tag not found exactly once"
assert "</script" not in lib.lower(), "library text would close the inline script"
page = page.replace(CDN, "<script>/* three.js r128, MIT License, Copyright 2010-2021 Three.js Authors */\n" + lib + "\n</script>")

head = (
    '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    "<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
    "body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style>\n"
    "</head>\n<body>\n"
)
OUT.write_text(head + page + "\n</body>\n</html>\n", encoding="utf-8")
print(f"{OUT}  {OUT.stat().st_size / 1024:.0f} KB")
