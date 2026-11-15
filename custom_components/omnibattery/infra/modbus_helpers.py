"""Generic Modbus helpers shared by the brand transport clients in drivers/."""

import inspect
from typing import Optional


def _detect_slave_kwarg(client) -> str:
    """Return the keyword pymodbus uses to address the slave/unit id.

    pymodbus renamed this parameter from ``slave`` to ``device_id`` across 3.x
    releases. Inspect the live method signature so we pass the right one
    regardless of the version Home Assistant bundles.
    """
    try:
        params = inspect.signature(client.read_holding_registers).parameters
        if "device_id" in params:
            return "device_id"
    except (ValueError, TypeError):
        pass
    return "slave"


def decode_registers(regs, data_type: str = "uint16", bit_index: Optional[int] = None):
    """Interpret a list of Modbus register words as a typed value.

    Shared by single-register reads and block reads (which slice the block
    buffer per field). Returns the decoded value, or None if ``regs`` is too
    short for the requested type. Raises ValueError for an unsupported
    data_type or an invalid bit_index.
    """
    if not regs:
        return None

    if data_type == "int16":
        val = regs[0]
        return val - 0x10000 if val >= 0x8000 else val

    elif data_type == "uint16":
        return regs[0]

    elif data_type == "int32":
        if len(regs) < 2:
            return None
        val = (regs[0] << 16) | regs[1]
        return val - 0x100000000 if val >= 0x80000000 else val

    elif data_type == "uint32":
        if len(regs) < 2:
            return None
        return (regs[0] << 16) | regs[1]

    elif data_type == "uint48":
        if len(regs) < 3:
            return None
        return (regs[0] << 32) | (regs[1] << 16) | regs[2]

    elif data_type == "uint64":
        if len(regs) < 4:
            return None
        return (regs[0] << 48) | (regs[1] << 32) | (regs[2] << 16) | regs[3]

    elif data_type == "char":
        byte_array = bytearray()
        for reg in regs:
            byte_array.append((reg >> 8) & 0xFF)
            byte_array.append(reg & 0xFF)
        text = byte_array.decode("ascii", errors="ignore").rstrip('\x00')
        return "".join(char for char in text if char.isprintable())

    elif data_type == "bit":
        if bit_index is None or not (0 <= bit_index < 16):
            raise ValueError("bit_index must be between 0 and 15 for bit data_type")
        return bool((regs[0] >> bit_index) & 1)

    else:
        raise ValueError(f"Unsupported data_type: {data_type}")
