"""Verify extraction deliverable hashes, optionally including retained source."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,help='Optional original Capstone root; not a runtime dependency')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    manifest=json.loads((root/'docs/extraction-manifest.json').read_text())
    checked=0
    for row in manifest['files']:
        path=(root/row['new_path']).resolve()
        if not path.is_relative_to(root):raise ValueError('Manifest path escapes project')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=row['new_sha256']:raise ValueError('Changed deliverable: '+row['new_path'])
        if args.source and row['original_path']:
            original=(args.source/row['original_path']).resolve()
            if not original.is_relative_to(args.source.resolve()):raise ValueError('Source path escapes project')
            if hashlib.sha256(original.read_bytes()).hexdigest()!=row['original_sha256']:raise ValueError('Changed original: '+row['original_path'])
        checked+=1
    print(json.dumps({'verified_files':checked,'source_checked':bool(args.source)}))


if __name__=='__main__':main()
