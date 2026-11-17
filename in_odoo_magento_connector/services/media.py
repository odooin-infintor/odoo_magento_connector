"""Shared idempotent image publishing for simple and configurable products."""
import base64
import hashlib
import json
from pathlib import PurePosixPath
from urllib.parse import quote

from odoo import _
from odoo.exceptions import UserError


def image_fingerprint(record, image):
    """Scope the content acknowledgement to this remote product and store."""
    return hashlib.sha256(json.dumps([
        record.instance_id.base_url, record.instance_id.store_code,
        record.external_id, record.sku, hashlib.sha256(base64.b64decode(image)).hexdigest(),
    ]).encode()).hexdigest()


def image_changed(record, image):
    return record.last_image_fingerprint != image_fingerprint(record, image)


def export_image(record, image, name):
    fingerprint = image_fingerprint(record, image)
    if record.last_image_fingerprint == fingerprint:
        return
    raw = base64.b64decode(image)
    if raw.startswith(b"\x89PNG"):
        mime, ext = "image/png", "png"
    elif raw.startswith(b"\xff\xd8"):
        mime, ext = "image/jpeg", "jpg"
    else:
        raise UserError(_("Only PNG and JPEG images are currently supported."))
    marker = "odoo-" + hashlib.sha256(raw).hexdigest()[:24]
    path = f"products/{quote(record.sku, safe='')}/media"
    with record.instance_id._client() as client:
        media = client.request("GET", path)
        if any(PurePosixPath(entry.get("file") or "").stem == marker
               or PurePosixPath(entry.get("file") or "").stem.startswith(marker + "_") for entry in media):
            record.last_image_fingerprint = fingerprint
            return
        # Magento may add directories or a numeric suffix to the filename.
        # Keep the content hash in the filename, not customer-facing alt text.
        result = client.request("POST", path, payload={"entry": {
            "media_type": "image", "label": name, "position": 0, "disabled": False,
            "types": ["image", "small_image", "thumbnail"],
            "content": {"base64_encoded_data": image, "type": mime, "name": f"{marker}.{ext}"},
        }})
        # Magento's REST layer serializes the new media entry ID as a JSON string ("7").
        if isinstance(result, str) and result.isdigit():
            result = int(result)
        if type(result) is not int or result < 1:
            raise UserError(_("Magento did not acknowledge the image. Retry to verify its media entry."))

    record.last_image_fingerprint = fingerprint

    record.env['in.magento.sync.log']._record(record, direction='export', operation='image',
        summary='Product image accepted by Magento.', after={'image_hash': marker})
