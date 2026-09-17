#!/usr/bin/env python3
"""Generate a self-contained HTML report from collected database JSON.

Usage:
    python3 scripts/generate_report.py <input_json> [output_html]

The script takes the pre-built viewer template (viewer/report-template.html)
and injects the database JSON data into it, producing a single file that can
be opened in any browser without a server.

If output_html is not specified, it defaults to <input_basename>_report.html
in the same directory as the input file.

Requirements: Python 3 (no additional packages needed)
"""
import json
import os
import sys


def generate_report(input_json: str, output_html: str = None, template_path: str = None):
    """Generate HTML report from JSON data file."""
    # Resolve template path
    if template_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(script_dir, '..', 'viewer', 'report-template.html')

    if not os.path.exists(template_path):
        print(f"ERROR: Template not found at {template_path}")
        print("The viewer/report-template.html file is required.")
        sys.exit(1)

    if not os.path.exists(input_json):
        print(f"ERROR: Input file not found: {input_json}")
        sys.exit(1)

    # Default output path
    if output_html is None:
        base = os.path.splitext(os.path.basename(input_json))[0]
        # Remove _data suffix if present
        if base.endswith('_data'):
            base = base[:-5]
        output_html = os.path.join(os.path.dirname(input_json), f"{base}_report.html")

    # Load JSON data
    print(f"Loading: {input_json}")
    with open(input_json) as f:
        data = json.load(f)

    # Extract metadata
    meta = {
        'database_id': data.get('database_id', data.get('cluster_id', 'unknown')),
        'collection_timestamp': data.get('collection_timestamp', ''),
        'database_type': data.get('database_type', 'unknown'),
    }

    # Load template
    with open(template_path) as f:
        template = f.read()

    # Inject data before the closing </head> tag
    inject_script = (
        f'<script>window.__DB_DATA__ = {json.dumps(data)};\n'
        f'window.__DB_META__ = {json.dumps(meta)};</script>\n'
    )

    # Update the title
    title = f"DB Report - {meta['database_id']}"
    template = template.replace('<title>DB Metrics Report</title>', f'<title>{title}</title>')

    # Inject before </head>
    template = template.replace('</head>', f'{inject_script}</head>')

    # Write output
    with open(output_html, 'w') as f:
        f.write(template)

    size_mb = os.path.getsize(output_html) / (1024 * 1024)
    print(f"Generated: {output_html} ({size_mb:.1f} MB)")
    print(f"Database: {meta['database_id']} ({meta['database_type']})")
    print(f"Open in any browser - no server required.")
    return output_html


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/generate_report.py <input_json> [output_html]")
        print("")
        print("Examples:")
        print("  python3 scripts/generate_report.py data/database-1_invasive_data.json")
        print("  python3 scripts/generate_report.py data/database-1_invasive_data.json my_report.html")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    generate_report(input_file, output_file)
