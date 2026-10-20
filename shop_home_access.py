"""Strict, uncached authorization for Shop sessions explicitly issued by Home."""
import json
import os
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def allowed(telegram_id: int) -> bool:
    url = os.environ.get('HOME_ACCESS_URL', '')
    token = os.environ.get('HOME_ACCESS_INTERNAL_TOKEN', '')
    if not url or not token:
        return False
    try:
        req = urllib.request.Request(url, data=json.dumps({'telegram_id': telegram_id, 'app_key': 'shop', 'require_entitlement': True}).encode(), headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method='POST')
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=1.5) as response:
            return response.status == 200 and json.load(response).get('decision') == 'allow'
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except Exception:
        return False
