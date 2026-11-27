"""
check_device.py - Phase 2 test: is my iPhone visible over USB?

It talks to Apple's usbmux service (Apple Devices app, 127.0.0.1:27015)
through pymobiledevice3, lists connected iPhones and prints
name / model / iOS version / UDID for each.

Run:
    receiver\\.venv\\Scripts\\python.exe receiver\\check_device.py
"""

import asyncio
import sys

from pymobiledevice3 import usbmux
from pymobiledevice3.lockdown import create_using_usbmux

PAIR_TIMEOUT_S = 60  # time you have to tap "Trust" on the iPhone


async def describe(device: usbmux.MuxDevice) -> None:
    """Open a lockdown session to one device and print its info."""
    print(f"\nDevice found via {device.connection_type}: {device.serial}")
    print("  Reading details (if the iPhone asks 'Trust This Computer?', tap Trust and enter your passcode)...")
    lockdown = await create_using_usbmux(
        serial=device.serial,
        autopair=True,
        pair_timeout=PAIR_TIMEOUT_S,
    )
    try:
        info = lockdown.short_info
        print(f"  Name        : {info.get('DeviceName')}")
        print(f"  Model       : {lockdown.display_name or info.get('ProductType')}")
        print(f"  iOS version : {info.get('ProductVersion')} ({info.get('BuildVersion')})")
        print(f"  UDID        : {info.get('UniqueDeviceID')}")
    finally:
        await lockdown.close()


async def main() -> int:
    try:
        devices = await usbmux.list_devices()
    except Exception as e:  # usbmux service not reachable
        print("ERROR: cannot talk to Apple Mobile Device service (usbmux).")
        print(f"  Details: {e!r}")
        print("  Fix: open the 'Apple Devices' app once, or reboot the laptop.")
        return 2

    usb_devices = [d for d in devices if d.is_usb]
    if not usb_devices:
        print("No iPhone found over USB.")
        print("  - Plug in the cable (try another USB port / cable)")
        print("  - Unlock the iPhone and tap 'Trust' if asked")
        print("  - Make sure the 'Apple Devices' app is installed")
        return 1

    ok = True
    for dev in usb_devices:
        try:
            await describe(dev)
        except Exception as e:
            ok = False
            print(f"  ERROR reading device: {type(e).__name__}: {e}")
            print("  Fix: unlock the iPhone, tap 'Trust', then run this again.")

    print("\nRESULT:", "iPhone detected OK" if ok else "device seen, but details failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
