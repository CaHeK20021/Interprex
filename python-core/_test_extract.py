import urllib.request, json, sys, io
out = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
data = json.dumps({'root': r'G:\5inchENG\13\AuntsHouse', 'engine': 'unity', 'sub_paths': []}).encode()
req = urllib.request.Request('http://127.0.0.1:8723/extract', data=data, headers={'Content-Type': 'application/json'})
resp = urllib.request.urlopen(req, timeout=300)
result = json.loads(resp.read())
strings = result.get('strings', [])
out.write(f'strings: {len(strings)}\n')
if strings:
    out.write(f'first: {strings[0]["original"][:80]}\n')
    out.write(f'last: {strings[-1]["original"][:80]}\n')
else:
    out.write('EMPTY!\n')
out.flush()
