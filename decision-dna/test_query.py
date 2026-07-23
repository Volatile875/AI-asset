#!/usr/bin/env python3
import urllib.request
import json

try:
    req = urllib.request.Request(
        'http://localhost:8004/query',
        data=json.dumps({'question': 'Auth Refactor Architecture Review'}).encode(),
        headers={'Content-Type': 'application/json'}
    )
    resp = urllib.request.urlopen(req, timeout=90)
    result = json.loads(resp.read().decode())
    print('✅ SUCCESS')
    print('Answer preview:', result.get('answer', 'N/A')[:300])
    print('\nFull response keys:', list(result.keys()))
except Exception as e:
    print(f'❌ ERROR: {type(e).__name__}')
    print(f'Details: {str(e)[:500]}')
