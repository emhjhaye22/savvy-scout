"""Microsoft Graph OneDrive file upload (2026-09-17 audit finding): the
daily SQLite backup previously lived only on the same Render disk as the
database it protects -- a single disk failure would take out both at once.
This uploads a copy to OneDrive using the SAME Azure AD app registration
already configured for escalation email (MS_GRAPH_TENANT_ID/CLIENT_ID/
CLIENT_SECRET, MS_GRAPH_SENDER_UPN's own OneDrive) -- it just needs
Files.ReadWrite.All added to that app registration's application
permissions in Azure Portal, an external setup step, same as Mail.Send was
for escalation email. Written and unit-testable before that permission is
granted; upload_file simply fails loudly if it isn't (see run_daily_backup's
handling in scheduler.py).

Backup files are large (100MB+) and only growing, well past the 4MB limit
for a single PUT, so this uses Graph's resumable upload session rather than
the simple content-upload endpoint mail.py's attachment flow uses."""

from pathlib import Path

import requests

from savvy_scout.graph.mail import GRAPH_BASE_URL, REQUEST_TIMEOUT_SECONDS, _get_access_token

# Must be a multiple of 320 KiB per Graph's requirement; 10 MiB balances
# request count against retry cost on a flaky connection.
CHUNK_SIZE = 10 * 1024 * 1024


def upload_file(
    file_path: str,
    remote_folder: str,
    upn: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> str:
    """Uploads file_path to {upn}'s OneDrive at remote_folder/<filename>,
    replacing any existing file of the same name. Returns the uploaded
    item's webUrl. Raises requests.HTTPError on any failure -- callers
    decide whether that should be fatal or just logged (see
    scheduler.run_daily_backup, which logs and continues rather than
    treating a Graph outage as a reason to skip the local backup)."""
    source = Path(file_path)
    size = source.stat().st_size
    token = _get_access_token(tenant_id, client_id, client_secret)

    session_resp = requests.post(
        f"{GRAPH_BASE_URL}/users/{upn}/drive/root:/{remote_folder}/{source.name}:/createUploadSession",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    session_resp.raise_for_status()
    upload_url = session_resp.json()["uploadUrl"]

    with source.open("rb") as f:
        start = 0
        result = None
        while start < size:
            chunk = f.read(CHUNK_SIZE)
            end = start + len(chunk) - 1
            # The upload session URL is itself pre-authenticated (it embeds
            # a short-lived token) -- no Authorization header on chunk PUTs.
            chunk_resp = requests.put(
                upload_url,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
                data=chunk,
                timeout=REQUEST_TIMEOUT_SECONDS * 4,  # chunks are much larger than a normal Graph call
            )
            chunk_resp.raise_for_status()
            start += len(chunk)
            if chunk_resp.status_code in (200, 201):
                result = chunk_resp.json()

    return result["webUrl"] if result else ""
