"""Preserve MQTT product variants when resolving cloud device metadata."""

from libdyson import DEVICE_TYPE_NAMES


def resolve_device_type(info):
    """Prefer explicit supported types over the library's base-type aliases.

    See libdyson-wg/ha-dyson#317 and #320. Existing config entries are not
    rewritten; this only resolves newly discovered cloud devices.
    """
    product_type = str(info.product_type or "").strip()
    if product_type in ("438", "527", "358"):
        variant = (getattr(info, "variant", None) or "").strip().upper()
        if not variant:
            version = getattr(info, "version", "") or ""
            if version.startswith(product_type) and len(version) > 3:
                variant = version[3].upper()
        candidate = product_type + variant
        if variant and candidate in DEVICE_TYPE_NAMES:
            return candidate
    if product_type in DEVICE_TYPE_NAMES:
        return product_type
    return info.get_device_type()
