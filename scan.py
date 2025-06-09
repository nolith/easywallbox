import asyncio
from bleak import BleakScanner

async def scan():
    print("Scanning for BLE devices...")
    devices = await BleakScanner.discover()
    if not devices:
        print("No BLE devices found.")
        return

    print("Found BLE devices:")
    for i, device in enumerate(devices, 1):
        print(f"{i}. Address: {device.address}, Name: {device.name}, RSSI: {device.rssi} dBm")

def main():
    asyncio.run(scan())

if __name__ == "__main__":
    main()
