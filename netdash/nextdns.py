import json
from typing import Any, Dict

import requests


def nextdns_status(test_url: str) -> Dict[str, Any]:
    try:
        r = requests.get(
            test_url,
            timeout=3,
            allow_redirects=True,
            headers={"Accept": "application/json"},
        )
        data = (
            r.json()
            if "application/json" in r.headers.get("content-type", "")
            else json.loads(r.text)
        )
        status = data.get("status")
        return {"reachable": True, "status": status, "using_nextdns": (status == "ok"), "raw": data}
    except Exception as e:
        return {"reachable": False, "status": "error", "using_nextdns": False, "error": str(e)}
