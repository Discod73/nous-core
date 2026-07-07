#!/bin/bash
echo "=== NOUS Session 1 Smoke-test ==="
echo ""

# 1. Qdrant health
curl -s http://localhost:6333/healthz | grep -q "passed" && echo "✅ 1/6 Qdrant health" || echo "❌ 1/6 Qdrant health"

# 2. Wings collections
python3 -c "
import json, requests
from pathlib import Path
cfg = Path('/srv/nous/config/wings.json')
if not cfg.exists():
    print('❌ 2/6 Wings: wings.json ikke fundet')
else:
    wings = [w['collection'] for w in json.loads(cfg.read_text()).get('wings', [])]
    r = requests.get('http://localhost:6333/collections')
    found = [c['name'] for c in r.json()['result']['collections']]
    missing = [w for w in wings if w not in found]
    print(f'✅ 2/6 Wings OK ({len(wings)} konfigureret)' if not missing else f'❌ 2/6 Wings: mangler {missing}')
"

# 3. Kuzu stub
cd /srv/nous/app && source .venv/bin/activate && python3 -c "import kuzu; db=kuzu.Database('/mnt/nous-data/kuzu', max_db_size=8589934592); conn=kuzu.Connection(db); r=conn.execute('MATCH (c:Claim) RETURN COUNT(c) AS n'); print('✅ 3/6 Kuzu stub' if r.get_next()[0]==0 else '❌ 3/6 Kuzu has data')" && deactivate

# 4. Firewall (drop policy)
sudo nft list ruleset | grep -q "policy drop" && echo "✅ 4/6 Firewall drop" || echo "❌ 4/6 Firewall"

# 5. tmpfs mounted
mount | grep -q "/mnt/ephemeral" && echo "✅ 5/6 tmpfs ephemeral" || echo "❌ 5/6 tmpfs"

# 6. Backup timer
systemctl is-active nous-backup.timer >/dev/null 2>&1 && echo "✅ 6/6 Backup timer" || echo "❌ 6/6 Backup timer"

echo ""
echo "=== Test færdig ==="
