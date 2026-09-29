import sys

version, old, entry = sys.argv[1], sys.argv[2], sys.argv[3]
p = "CHANGELOG.md"
s = open(p, encoding="utf-8").read()
head = s.index("## ")
s = s[:head] + entry.replace("\\n", "\n") + "\n" + s[head:]
open(p, "w", encoding="utf-8").write(s)
p = "custom_components/ontology/manifest.json"
s = open(p, encoding="utf-8").read()
assert f'"version": "{old}"' in s, s
open(p, "w", encoding="utf-8").write(s.replace(f'"version": "{old}"', f'"version": "{version}"'))
print("bumped", old, "->", version)
