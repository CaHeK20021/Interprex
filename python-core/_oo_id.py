import hashlib, re

what_raw = '“The mayo in your fridge will expire tomorrow.”'

def encode_say_string(s):
    s = s.replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')
    s = re.sub(r'(?<= ) ', '\\ ', s)
    return '"' + s + '"'

code = encode_say_string(what_raw)
digest = hashlib.md5((code + '\r\n').encode('utf-8')).hexdigest()[:8]
print('code =', repr(code))
print('digest =', digest)
print('id = morning_1_' + digest)
