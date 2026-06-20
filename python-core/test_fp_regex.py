import re

FMT = re.compile(r'%(?:%|\(\w+\)[diouxXeEfFgGcrs]|[-#0+]*\d*\.?\d*[hlL]?[diouxXeEfFgGcrs])')

fp_tests = [
    ('%s is ready', '%s готов', 'fmt preserved with space'),
    ('{b}[name] has %s items{/b}', '{b}[name] имеет %s вещей{/b}', 'all preserved'),
    ('100% done', '100% готово', 'percent in text'),
    ('Score: %d', 'Очки: %d', 'only format'),
    ('%+10d items', '%+10d вещей', 'percent with flags'),
    ('%s is %d items', '%s это %d вещей', 'multiple formats'),
]

tp_tests = [
    ('%s ready', 'готов', 'percent-s dropped'),
    ('%d items', 'вещи', 'percent-d dropped'),
    ('%(name)s hi', 'привет', 'named format dropped'),
    ('%% done', '% готово', 'escaped percent corrupted'),
]

ok = 0
fail = 0
for old, new, desc in fp_tests:
    old_f = sorted(FMT.findall(old))
    new_f = sorted(FMT.findall(new))
    passed = old_f == new_f
    tag = "PASS" if passed else "FAIL"
    print("  %s: %s  old=%s new=%s" % (tag, desc, old_f, new_f))
    if passed:
        ok += 1
    else:
        fail += 1

for old, new, desc in tp_tests:
    old_f = sorted(FMT.findall(old))
    new_f = sorted(FMT.findall(new))
    passed = old_f != new_f
    tag = "PASS" if passed else "FAIL"
    print("  %s: %s  old=%s new=%s" % (tag, desc, old_f, new_f))
    if passed:
        ok += 1
    else:
        fail += 1

print("\nTotal: %d passed, %d failed" % (ok, fail))
if fail:
    exit(1)
