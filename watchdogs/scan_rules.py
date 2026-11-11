"""Existing discovery rewards shared by live scans and verified imports."""


def observation_xp(protocol, new_identity):
    if protocol not in ("wifi", "ble") or type(new_identity) is not bool:
        raise ValueError("Invalid observation reward input")
    return (15 if protocol == "wifi" else 10) if new_identity else 1
