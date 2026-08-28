import pathlib

fixes = {
    'â‚¹': '₹',
    'â€”': '—',
    'âœ“': '✓',
    'âœ—': '✗',
    'Â·': '·',
    'â–¶': '▶',
    'ðŸŒ±': '🌱',
    'ðŸ’¥': '💥',
    'â€œ': '“',
    'â€': '”',
    'âˆ’': '−', # minus
    'âœ”': '✔'
}

for filename in ['dashboard/app.js', 'dashboard/index.html', 'dashboard/style.css']:
    path = pathlib.Path(filename)
    text = path.read_text(encoding='utf-8')
    for bad, good in fixes.items():
        text = text.replace(bad, good)
    path.write_text(text, encoding='utf-8')
print('Fixes applied.')
