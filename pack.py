#!/usr/bin/env python3
"""
Pack a Construct 2 export into a single HTML file using a binary virtual filesystem.
Assets are stored as a single Uint8Array blob, parsed once at load time into a Map.
"""

import os
import re
import struct
import json
import base64
from pathlib import Path

GAME_DIR = Path(__file__).parent
OUTPUT_FILE = GAME_DIR / "ovo-packed.html"

MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".js": "application/javascript", ".css": "text/css",
    ".json": "application/json", ".txt": "text/plain",
}

def get_mime(path: Path) -> str:
    return MIME_MAP.get(path.suffix.lower(), "application/octet-stream")

def should_embed(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() == ".html":
        return False
    if path.name in ("pack.py",):
        return False
    return True

def build_vfs_blob(game_dir: Path) -> tuple[bytes, dict]:
    """
    Binary format per entry:
      [4 bytes] filename length (uint32 LE)
      [N bytes] filename (utf-8)
      [4 bytes] mime length (uint32 LE)
      [M bytes] mime type (utf-8)
      [4 bytes] data length (uint32 LE)
      [K bytes] raw file data
    """
    parts = []
    manifest = {}

    for filepath in sorted(game_dir.rglob("*")):
        if not should_embed(filepath):
            continue

        rel = filepath.relative_to(game_dir).as_posix()
        mime = get_mime(filepath)
        data = filepath.read_bytes()

        name_bytes = rel.encode("utf-8")
        mime_bytes = mime.encode("utf-8")

        parts.append(struct.pack("<I", len(name_bytes)))
        parts.append(name_bytes)
        parts.append(struct.pack("<I", len(mime_bytes)))
        parts.append(mime_bytes)
        parts.append(struct.pack("<I", len(data)))
        parts.append(data)

        manifest[rel] = mime
        print(f"  {rel} ({len(data):,} bytes)")

    return b"".join(parts), manifest


VFS_SCRIPT = r"""
<script>
(function() {
    // Decode the binary VFS blob (base64 -> binary -> parse)
    const b64 = window.__VFS_B64__;
    const raw = atob(b64);
    const blob = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) blob[i] = raw.charCodeAt(i);

    // Parse into a Map: filename -> { mime, data: Uint8Array }
    const vfs = new Map();
    const dec = new TextDecoder();
    let pos = 0;

    function readU32() {
        const v = (blob[pos]) | (blob[pos+1]<<8) | (blob[pos+2]<<16) | (blob[pos+3]<<24);
        pos += 4;
        return v >>> 0;
    }
    function readBytes(n) { return blob.subarray(pos, (pos += n)); }
    function readStr(n)   { return dec.decode(readBytes(n)); }

    while (pos < blob.length) {
        const name = readStr(readU32());
        const mime = readStr(readU32());
        const data = readBytes(readU32());
        vfs.set(name, { mime, data });
    }

    window.__VFS__ = vfs;
    console.log('[VFS] Loaded', vfs.size, 'files');

    // --- Helpers ---
    function getEntry(url) {
        const clean = (typeof url === 'string' ? url : String(url)).split('?')[0].replace(/^\//, '');
        return vfs.get(clean) || null;
    }
    function toObjectURL(entry) {
        return URL.createObjectURL(new Blob([entry.data], { type: entry.mime }));
    }
    function toText(entry) {
        return new TextDecoder().decode(entry.data);
    }

    // --- fetch ---
    const _fetch = window.fetch;
    window.fetch = function(url, opts) {
        const entry = getEntry(url);
        if (entry) {
            return Promise.resolve(new Response(
                new Blob([entry.data], { type: entry.mime }),
                { status: 200, headers: { 'Content-Type': entry.mime } }
            ));
        }
        return _fetch(url, opts);
    };

    // --- XMLHttpRequest ---
    const _XHR = window.XMLHttpRequest;
    window.XMLHttpRequest = function() {
        const xhr = new _XHR();
        let _entry = null;

        const _open = xhr.open.bind(xhr);
        xhr.open = function(method, url, ...rest) {
            _entry = getEntry(url);
            if (_entry) console.log('[VFS] XHR intercept:', url);
            return _open(method, url, ...rest);
        };

        Object.defineProperty(xhr, 'responseType', {
            get() { return this._rt || ''; },
            set(v) { this._rt = v; },
            configurable: true
        });

        const _send = xhr.send.bind(xhr);
        xhr.send = function(...args) {
            if (!_entry) return _send(...args);
            const self = this;
            const entry = _entry;
            setTimeout(function() {
                const text = toText(entry);
                Object.defineProperty(self, 'status',       { value: 200,  writable: true });
                Object.defineProperty(self, 'statusText',   { value: 'OK', writable: true });
                Object.defineProperty(self, 'readyState',   { value: 4,    writable: true });
                Object.defineProperty(self, 'responseText', { value: text, writable: true });
                Object.defineProperty(self, 'response', {
                    value: self._rt === 'json'        ? JSON.parse(text)
                         : self._rt === 'arraybuffer' ? entry.data.buffer.slice(entry.data.byteOffset, entry.data.byteOffset + entry.data.byteLength)
                         : text,
                    writable: true
                });
                if (typeof self.onreadystatechange === 'function') self.onreadystatechange();
                if (typeof self.onload === 'function') self.onload();
            }, 0);
        };

        return xhr;
    };

    // --- setAttribute (images, dynamic scripts) ---
    const _setAttr = Element.prototype.setAttribute;
    Element.prototype.setAttribute = function(name, value) {
        if (name === 'src' && value && !value.startsWith('data:') && !value.startsWith('http') && !value.startsWith('blob:')) {
            const entry = getEntry(value);
            if (entry) value = toObjectURL(entry);
        }
        return _setAttr.call(this, name, value);
    };

    // --- img.src property (Construct 2 sets this directly, bypassing setAttribute) ---
    const _srcDesc = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
    Object.defineProperty(HTMLImageElement.prototype, 'src', {
        get: function() { return _srcDesc.get.call(this); },
        set: function(value) {
            if (value && !value.startsWith('data:') && !value.startsWith('http') && !value.startsWith('blob:')) {
                const entry = getEntry(value);
                if (entry) value = toObjectURL(entry);
            }
            _srcDesc.set.call(this, value);
        },
        configurable: true
    });

    // --- CSS link tags ---
    const _linkSetAttr = HTMLLinkElement.prototype.setAttribute;
    HTMLLinkElement.prototype.setAttribute = function(name, value) {
        if (name === 'href' && value && !value.startsWith('data:') && !value.startsWith('http')) {
            const entry = getEntry(value);
            if (entry && entry.mime === 'text/css') {
                const style = document.createElement('style');
                style.textContent = toText(entry);
                document.head.appendChild(style);
                return;
            }
        }
        return _linkSetAttr.call(this, name, value);
    };

})();
</script>
"""

def inline_scripts(html: str, game_dir: Path) -> str:
    """Replace static <script src="..."> tags with inline <script> blocks."""
    def replace(match):
        src = match.group(1).split('?')[0]
        path = game_dir / src
        if path.is_file():
            print(f"  Inlining: {src}")
            return f'<script>{path.read_text(encoding="utf-8", errors="replace")}</script>'
        return match.group(0)
    return re.sub(r'<script\s+src="([^"]+)"[^>]*>\s*</script>', replace, html)

def main():
    print(f"Packing {GAME_DIR} ...")

    print("\nBuilding VFS blob...")
    blob, manifest = build_vfs_blob(GAME_DIR)
    b64 = base64.b64encode(blob).decode("ascii")
    print(f"\nVFS: {len(manifest)} files, {len(blob):,} raw bytes, {len(b64):,} base64 chars")

    html = (GAME_DIR / "index.html").read_text(encoding="utf-8-sig")

    # Cleanups
    html = re.sub(r'<script>[\s\S]*?window\.location\.protocol[\s\S]*?</script>', '', html)
    html = re.sub(r'<link rel="manifest"[^>]*>', '', html)
    html = re.sub(r'<link rel="apple-touch-icon"[^>]*>', '', html)
    html = re.sub(r'<link rel="shortcut icon"[^>]*>', '', html)
    html = re.sub(r'<meta name="mobile-web-app-capable"[^/]*/>', '', html)

    # Inject blob + VFS runtime after <head>
    head_pos = html.find("<head>") + 6
    blob_tag = f'\n<script>window.__VFS_B64__ = "{b64}";</script>\n'
    html = html[:head_pos] + blob_tag + VFS_SCRIPT + html[head_pos:]

    # Inline static <script src="..."> tags
    print("\nInlining scripts...")
    html = inline_scripts(html, GAME_DIR)

    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"\nDone! -> {OUTPUT_FILE}")
    print(f"Output size: {OUTPUT_FILE.stat().st_size:,} bytes")

if __name__ == "__main__":
    main()