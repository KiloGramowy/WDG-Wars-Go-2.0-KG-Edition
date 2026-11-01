"""Shared mesh discovery rule extracted from the existing MeshCore UI.

The original app awarded 20 XP for a newly discovered persistent MeshCore
contact, without a GPS requirement. Both mesh protocols use that one rule.
No independent Meshtastic rates or score table are introduced here.
"""

MESH_NEW_CONTACT_XP = 20
NEW_CONTACT_XP = MESH_NEW_CONTACT_XP


def mesh_contact_xp(*, new_event: bool) -> int:
    return MESH_NEW_CONTACT_XP if new_event else 0
