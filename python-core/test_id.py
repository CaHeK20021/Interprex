import sys, hashlib
sys.path.insert(0, '.')
from parsers.renpy import _encode_say_string

who = 'mc'
what = "Being an aspiring crime writer hasn't been working out for you."
encoded = _encode_say_string(what)
get_code = f'{who} {encoded}'
ident = hashlib.md5((get_code + '\r\n').encode('utf-8')).hexdigest()[:8]
label = 'intro_monologue'
full = f'{label}_{ident}'
expected = 'intro_monologue_084cf155'

sys.stdout.buffer.write(f'encoded: {encoded}\n'.encode('utf-8'))
sys.stdout.buffer.write(f'get_code: {get_code}\n'.encode('utf-8'))
sys.stdout.buffer.write(f'computed: {full}\n'.encode('utf-8'))
sys.stdout.buffer.write(f'expected: {expected}\n'.encode('utf-8'))
sys.stdout.buffer.write(f'match: {full == expected}\n'.encode('utf-8'))
