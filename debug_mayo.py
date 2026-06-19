import hashlib, re

# The source line as seen by the engine
# ""The mayo in your fridge will expire tomorrow.""
# Say with no speaker (empty who), text has curly quotes inside

what = "\u201cThe mayo in your fridge will expire tomorrow.\u201d"
print("what:", repr(what))

# encode_say_string:
# 1. s.replace("\\","\\\\").replace("\n","\\n").replace('"','\\"')
#    Note: \u201c and \u201d are NOT ASCII double quotes, so no escaping
s = what.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
# 2. re.sub(r'(?<= ) ', '\\ ', s) - escape 2nd consecutive space
s = re.sub(r'(?<= ) ', '\\ ', s)
# 3. wrap in quotes
encoded = '"' + s + '"'
print("encoded:", repr(encoded))

# For bare say (no who, no attrs), get_code = encoded
get_code = encoded
print("get_code:", repr(get_code))

# digest = md5((get_code + "\r\n").encode("utf-8")).hexdigest()[:8]
raw = (get_code + "\r\n").encode("utf-8")
digest = hashlib.md5(raw).hexdigest()[:8]
print("computed digest:", digest)

# TL has: morning_1_f5004a22
tl_digest = "f5004a22"
print("TL digest:", tl_digest)
print("Match:", digest == tl_digest)
