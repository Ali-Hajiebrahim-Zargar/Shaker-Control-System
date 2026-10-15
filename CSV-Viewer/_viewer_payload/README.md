# Shaker CSV Viewer payload

These three files contain the compressed, Base64-encoded implementation used by `../shaker_csv_viewer.py`.

The launcher concatenates the parts, decodes and decompresses them, then verifies the reconstructed source before execution:

- expected source size: `77,403` bytes
- expected SHA-256: `f44bc61b6c5c8ff2f12664e8b71e2b3a17fc4b2d9717571b5710209cc03e3fc0`

Do not edit the payload parts individually. Run the viewer normally:

```bash
python CSV-Viewer/shaker_csv_viewer.py
```

This packaging preserves the complete Shaker CSV Viewer 1.7 implementation, including chunked loading for large oscilloscope CSVs and WebGL plotting.
