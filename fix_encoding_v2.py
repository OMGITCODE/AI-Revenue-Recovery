import os

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
    'âˆ’': '−',
    'âœ”': '✔',
    'â˜€ï¸ ': '☀️'
}

for filename in ['dashboard/app.js', 'dashboard/index.html', 'dashboard/style.css']:
    with open(filename, 'r', encoding='utf-8') as f:
        text = f.read()
    
    for bad, good in fixes.items():
        text = text.replace(bad, good)
        
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(text)

print('Replacement complete.')
