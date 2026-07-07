import re

# Læs filen
with open("/srv/nous/pipeline/ingest.py", "r") as f:
    content = f.read()

# Erstat get_wing funktionen
old_func = '''def get_wing(filepath):
    """Bestem wing ud fra mappestruktur"""
    path = Path(filepath)
    parts = path.parts
    
    for part in parts:
        lower = part.lower()
        if lower in WING_MAP:
            return WING_MAP[lower]
    
    # Default
    log(f"Ingen wing-match for {filepath}, bruger nous_projekt/PRIVATE")
    return ("nous_projekt", "PRIVATE")'''

new_func = '''def get_wing(filepath):
    """Bestem wing ud fra mappestruktur under /home/nous/incoming/"""
    path = Path(filepath)
    
    # Kun tjek under incoming/
    try:
        rel = path.relative_to("/home/nous/incoming/")
        parts = rel.parts
    except ValueError:
        parts = path.parts
    
    for part in parts:
        lower = part.lower()
        if lower in WING_MAP:
            return WING_MAP[lower]
    
    # Default
    log(f"Ingen wing-match for {filepath}, bruger nous_projekt/PRIVATE")
    return ("nous_projekt", "PRIVATE")'''

content = content.replace(old_func, new_func)

with open("/srv/nous/pipeline/ingest.py", "w") as f:
    f.write(content)

print("get_wing() fixet")
