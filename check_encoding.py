bad_markers = [
    '\u00e2\u0082\u00b9',   # â‚¹ = corrupted ₹
    '\u00e2\u0080\u0093',   # â€" = corrupted —
    '\u00c3\u0082\u00c2\u00b7',  # Â· = corrupted ·
    '\u00c3\u00b0\u00c5\u00b8\u00c5\u2019\u00c2\u00b1',  # ðŸŒ± = corrupted 🌱
    '\u00e2\u0098\u0080',   # â˜€ = corrupted ☀
]

text = open('dashboard/app.js', encoding='utf-8').read()

# Simpler: just look for multi-byte latin1-misread sequences
# These all start with Ã or â followed by high bytes
import re
# Check for common corruption patterns
patterns = ['\u00e2\u0082', '\u00e2\u0080', '\u00c3\u0082', '\u00c3\u00b0', '\u00e2\u0098', '\u00e2\u0086', '\u00c2\u00a6']
found = []
for p in patterns:
    if p in text:
        found.append(repr(p))

if found:
    print('Still has corruption patterns:', found)
else:
    print('CLEAN. Rupee count:', text.count('\u20b9'))
