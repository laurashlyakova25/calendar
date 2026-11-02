import sys, pathlib
sys.argv.append("--offline")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "collector"))
import build

lines = build.html_to_lines((pathlib.Path(__file__).parent / "crocus_sample.html").read_text())
evs = build.parse_crocus_lines(lines)
for e in evs:
    print(e["start"], e["end"], e["title"], "|", e["detail"], "| w", e["base_weight"], "->", e["weight"], "| km", e["km"])
assert [e["title"] for e in evs] == ["MITEX", "Lift Expo. Moscow", "MEBEL-2026"], evs
assert evs[0]["base_weight"] == 6 and evs[1]["base_weight"] == 2 and evs[2]["base_weight"] == 4
print("OK")
