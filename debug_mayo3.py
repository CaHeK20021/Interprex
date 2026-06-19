import hashlib, re

what = "\u201cThe mayo in your fridge will expire tomorrow.\u201d"

# New algorithm hypothesis: md5('speaker|text')
h1 = hashlib.md5(("None|" + what).encode("utf-8")).hexdigest()[:8]
print("md5(None|text):", h1)

h2 = hashlib.md5(("|" + what).encode("utf-8")).hexdigest()[:8]
print("md5(|text):", h2)

# With encoded quotes
h3 = hashlib.md5(('None|" ' + what + '"').encode("utf-8")).hexdigest()[:8]
print('md5(None|"text"):', h3)

# Original algorithm
s = what.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
s = re.sub(r'(?<= ) ', '\\ ', s)
get_code = '"' + s + '"'
digest = hashlib.md5((get_code + "\r\n").encode("utf-8")).hexdigest()[:8]
print("Original (get_code):", digest)
print()
print("Target: f5004a22")
