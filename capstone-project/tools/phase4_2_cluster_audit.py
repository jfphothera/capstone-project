"""Read-only inventory and bounded real qualification on authorized heads."""
import argparse
import concurrent.futures
import datetime
import ipaddress
import json
import time
import urllib.request
import os
from pathlib import Path


def request(endpoint, path, body=None, timeout=15):
    if os.environ.get('CAPSTONE_INFERENCE_MODE')=='gateway' and path in {'/api/generate','/api/chat','/api/embed'}:
        raise PermissionError('Raw model diagnostics are disabled in gateway mode; use the infrastructure maintenance qualification command')
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(endpoint + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def audit(address, timeout, preferred_model=None, endpoint=None):
    endpoint = endpoint or 'http://' + address + ':11434'
    result = {'address': address, 'endpoint': endpoint, 'actual_execution': True,
              'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'hardware_metrics': {'cpu_utilization': None, 'ram_available': None,
                                   'gpu_utilization': None, 'vram_available': None},
              'state': 'discovered'}
    started = time.monotonic()
    try:
        result['version'] = request(endpoint, '/api/version')
        result['inventory'] = request(endpoint, '/api/tags')['models']
        result['residency_before'] = request(endpoint, '/api/ps')
        result['state'] = 'enrolling'
        # Inspect metadata before selecting any generation model.
        metadata = {}
        for model in result['inventory']:
            tag = model['name']
            try:
                metadata[tag] = request(endpoint, '/api/show', {'model': tag})
            except Exception as exc:
                metadata[tag] = {'error': str(exc)}
        result['metadata'] = metadata
        candidates = [m for m in result['inventory']
                      if 'completion' in metadata[m['name']].get('capabilities', [])]
        candidates.sort(key=lambda m: m.get('size', float('inf')))
        if preferred_model:
            candidates = [m for m in candidates if m['name'] == preferred_model]
        if not candidates:
            raise ValueError('No metadata-confirmed completion model')
        selected = candidates[0]
        result['model'] = selected['name']
        result['digest'] = selected['digest']
        begin = time.monotonic()
        result['inference_started_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result['response'] = request(endpoint, '/api/generate', {
            'model': selected['name'], 'prompt': 'Return exactly the word READY.',
            'stream': False, 'think': False, 'keep_alive': '2m',
            'options': {'num_predict': 24, 'num_ctx': 1024, 'temperature': 0, 'seed': 42}}, timeout)
        result['inference_ms'] = round((time.monotonic() - begin) * 1000, 3)
        result['inference_finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        response = result['response']
        result['generation_completed'] = bool(response.get('done') and str(response.get('response', '')).strip())
        result['expected'] = 'READY'
        result['correct'] = str(response.get('response', '')).strip() == 'READY'
        result['state'] = 'qualified' if result['generation_completed'] and result['correct'] else 'degraded'
        embeddings = [m for m in result['inventory']
                      if 'embedding' in metadata[m['name']].get('capabilities', [])]
        if embeddings:
            embedding = min(embeddings, key=lambda m: m.get('size', float('inf')))
            start = time.monotonic()
            value = request(endpoint, '/api/embed', {
                'model': embedding['name'], 'input': 'Synthetic qualification fixture.'}, timeout)
            result['embedding'] = {'model': embedding['name'], 'digest': embedding['digest'],
                'duration_ms': round((time.monotonic()-start)*1000, 3),
                'dimensions': len(value.get('embeddings', [[]])[0]),
                'verified': bool(value.get('embeddings', [[]])[0])}
        result['residency_after'] = request(endpoint, '/api/ps')
    except Exception as exc:
        result['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        result['state'] = 'degraded' if 'inventory' in result else 'unreachable'
    result['duration_ms'] = round((time.monotonic()-started)*1000, 3)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--network', default='192.0.2.0/24')
    parser.add_argument('--addresses', default=','.join('192.0.2.'+str(n) for n in range(2, 11)))
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--preferred-model', help='Explicit qualification candidate; no fallback when missing')
    parser.add_argument('--output', default='docs/phase4_2/live_cluster_audit.json')
    args = parser.parse_args()
    network = ipaddress.ip_network(args.network)
    addresses = args.addresses.split(',')
    if not network.is_private or any(ipaddress.ip_address(a) not in network for a in addresses):
        raise ValueError('Every target must belong to the explicitly authorized private network')
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(9, len(addresses))) as pool:
        results = list(pool.map(lambda a: audit(a, args.timeout, args.preferred_model), addresses))
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'qualification_version': '4.2-v1', 'nodes': results}, indent=2))
    for result in results:
        print(result['address'], result['state'], result.get('model'), result.get('inference_ms'), result.get('error'))


if __name__ == '__main__':
    main()
