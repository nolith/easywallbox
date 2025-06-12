#!/usr/bin/env python3
import asyncio
import sys
from bleak import BleakClient
import aiomqtt
import os
import commands
import mqttmap
import time
import signal
import logging
import settings
from repeating_timer import RepeatingTimer
from blemap import WALLBOX_EPROM as eeprom
import blemap

import threading

_cleanup_lock = threading.Lock()
_cleanup_ran = False

FORMAT = ('%(asctime)-15s %(threadName)-15s '
          '%(levelname)-8s %(module)-15s:%(lineno)-8s %(message)s')
logging.basicConfig(format=FORMAT)
log = logging.getLogger()
log.setLevel(logging.DEBUG)

TOPIC = os.getenv('WALLBOX_TOPIC', 'easywallbox')
HOME_ASSISTANT_DISCOVERY = os.getenv('HOME_ASSISTANT_DISCOVERY', 'homeassistant')

EVCC_STATUS_SOURCE = f"{TOPIC}/ble/AD/4"
EVCC_ENABLED_SOURCE = f"{TOPIC}/ble/IDX/158" # Wallbox DPM limit
EVCC_COMMAND_MAXCURRENT = f"{TOPIC}/evcc/maxcurrent"
EVCC_COMMAND_MAXCURRENTMILLIS = f"{TOPIC}/evcc/maxcurrentmillis"
EVCC_COMMAND_ENABLE = f"{TOPIC}/evcc/enable"
EVCC_STATUS_AVAILABLE = "A"
EVCC_STATUS_VEHICLE_PRESENT = "B"
EVCC_STATUS_CHARGING = "C"
EVCC_STATUS_ERROR = "D"

settings = settings.Settings()

class EasyWallbox:
    WALLBOX_ADDRESS = os.getenv('WALLBOX_ADDRESS', 'demo')
    WALLBOX_PIN = os.getenv('WALLBOX_PIN', '9844')

    WALLBOX_RX = "a9da6040-0823-4995-94ec-9ce41ca28833";
    WALLBOX_SERVICE = "331a36f5-2459-45ea-9d95-6142f0c4b307";
    WALLBOX_ST = "75A9F022-AF03-4E41-B4BC-9DE90A47D50B";
    WALLBOX_TX = "a73e9a10-628f-4494-a099-12efaf72258f";
    WALLBOX_UUID ="0A8C44F5-F80D-8141-6618-2564F1881650";

    BLE_STATE_DISCONNECTED =    "disconnected";
    BLE_STATE_CONNECTING =       "connecting";
    BLE_STATE_CONNECTED =        "connected";
    BLE_STATE_CONNECTED_PAIRED =   "paired";
    BLE_STATE_CONNECTED_AUTH =   "authenticated";
    BLE_STATE_TERMINATING =      "terminating";

    def __init__(self, queue, mqtt_client):
        self._client = BleakClient(self.WALLBOX_ADDRESS)
        self._queue = queue
        self._lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self.state_timer = RepeatingTimer(self._update_ble_state)
        self._evcc_enabled = True
        self._mqtt_client = mqtt_client
        self._state = self.BLE_STATE_DISCONNECTED
        self._is_dpm_enabled = False
        self._rssi = 0

    def is_demo(self):
        return self.WALLBOX_ADDRESS == "demo"

    async def state(self):
        async with self._state_lock:
            return self._state

    async def _set_state(self, state):
        async with self._state_lock:
            self._state = state
            await self._mqtt_client.publish(f"{TOPIC}/status/ble", state, qos=1)

    async def is_connected(self):
        if self.is_demo():
            return await self.state() != self.BLE_STATE_DISCONNECTED
        return self._client.is_connected()

    async def is_connecting(self):
        return await self.state() == self.BLE_STATE_CONNECTING

    async def stop(self):
      await self._queue.put(commands.logout())
      await self._set_state(self.BLE_STATE_TERMINATING)
      async with self._lock:
        self.state_timer.stop()
        if not self.is_demo():
            await self._client.disconnect()

    async def write(self, data):
        if isinstance(data, str):
            data = bytearray(data, 'utf-8')
        if not self.is_demo():
            await self._client.write_gatt_char(self.WALLBOX_RX, data)
        log.debug("ble write: %s", data)

    async def _pair(self):
        state = await self.state()
        if state != self.BLE_STATE_CONNECTED:
            log.warn("_pair called in state %s", state)
            return

        await self._set_state(self.BLE_STATE_CONNECTED_PAIRED)
        log.info("Pairing BLE...")
        paired = sys.platform == "darwin" or self.is_demo() or await self._client.pair(protection_level=2)
        log.info(f"Paired: {paired}")
        await self._set_state(self.BLE_STATE_CONNECTED_PAIRED)

        if self.is_demo():
            return

        await self._client.start_notify(self.WALLBOX_TX, self._notification_handler_rx) #TX NOTIFY
        log.info("TX NOTIFY STARTED")
        await self._client.start_notify(self.WALLBOX_ST, self._notification_handler_st) #ST NOTIFY (CANAL BUSMODE)
        log.info("ST NOTIFY STARTED")

    async def _auth(self):
        state = await self.state()
        if state != self.BLE_STATE_CONNECTED_PAIRED:
            log.warn("_auth called in state %s", state)
            return

        log.info("BLE AUTH START: %s", self.WALLBOX_PIN)
        await self.write(commands.authBle(self.WALLBOX_PIN))
        if self.is_demo():
            await asyncio.sleep(1)
            await self._auth_completed()

    async def _auth_completed(self):
        state = await self.state()
        if state != self.BLE_STATE_CONNECTED_PAIRED:
            log.warn("_auth_completed called in state %s", state)
            return

        await self._set_state(self.BLE_STATE_CONNECTED_AUTH)
        log.info("BLE AUTH COMPLETE")
        # TODO pull the settings
        log.info("Starting ble state timer...")
        self.state_timer.start(await settings.get(settings.KEY_BLE_REFRESH_INTERVAL))

    async def connect(self):
        log.info("Connecting......")
        state = await self.state()
        log.info(f"State {state}")
        if state != self.BLE_STATE_DISCONNECTED:
            log.warn("Connect called in state %s", state)
            return

        try:
            await self._set_state(self.BLE_STATE_CONNECTING)
            log.info("Connecting BLE...")
            if not self.is_demo():
                await self._client.connect()
            log.info(f"Connected on {self.WALLBOX_ADDRESS}: {await self.is_connected()}")
            await self._set_state(self.BLE_STATE_CONNECTED)
            await self._pair()
            await self._auth()

        except Exception as e:
            log.error(f"Failed to connect or pair to device {self.WALLBOX_ADDRESS}: {e}")
            retry_in_sec = await settings.get(settings.KEY_BLE_RETRY_INTERVAL)
            log.error("Connection attempts failed. Retrying in {} seconds.".format(retry_in_sec))
            await asyncio.sleep(retry_in_sec)
            await self._set_state(self.BLE_STATE_DISCONNECTED)

    async def _update_ble_state(self):
        log.info("update ble state...")
        await self.read_application_data()
        log.info("ble state update done!")

    async def _enqueue_ble_command(self, cmd_key):
        log.info("_enqueue_ble_command %s", cmd_key)
        ble_cmd = eeprom[cmd_key]
        if ble_cmd is None:
            log.error(f"Unknown command key: {cmd_key}")
        else:
            log.info("Enqueue BLE cmd %s", ble_cmd)
        await self._queue.put(ble_cmd)

    async def read_application_data(self):
        await self._enqueue_ble_command("READ_APP_DATA")

    async def read_voltage(self):
        await self._enqueue_ble_command("READ_SUPPLY_VOLTAGE")

    async def evcc_enable(self, enable=True):
        log.warn("EVCC enable")
        if await self.state() != self.BLE_STATE_CONNECTED_AUTH:
            log.warn("Connection not authorized, ignoring evcc enable command")
            return

        if enable:
            await self._queue.put(commands.setDpmOff())
        else:
            # this is very tricky - I could not find a way to disable/pause a charge process
            # so the only way is set the limit to 1A so that the DPM will not allow the charge to start
            await self._queue.put(commands.setDpmLimit(1))
            await self._queue.put(commands.setDpmOn())

    def is_evcc_enabled(self):
        return not self._is_dpm_enabled

    _notification_buffer_rx = ""
    async def _notification_handler_rx(self, sender, data):
        self._notification_buffer_rx += data.decode()
        if "\n" in self._notification_buffer_rx:
            log.info("_notification RX received: %s", self._notification_buffer_rx)
            try:
                await self._mqtt_client.publish(topic=f"{TOPIC}/message", payload=self._notification_buffer_rx, qos=0, retain=False)

                match self._notification_buffer_rx:
                    case blemap.ANSWER_AUTHOK:
                        await self._auth_completed()
                    case blemap.ANSWER_LOGOUT:
                        state = await self.state()
                        if state != self.BLE_STATE_TERMINATING:
                            log.warn("Unexpected logout")
                            await self._auth()
                    case _:
                        chunks = self._notification_buffer_rx.split(",")
                        if len(chunks) > 3:
                            key = chunks[2]
                            data = chunks[3:]
                            if key == "AD" and len(data) > 8:
                                dpm_status = data[8]
                                log.debug(f"DPM status: {dpm_status}")
                                self._is_dpm_enabled = (dpm_status != "0")
                            if key in ["AL", "SL", "IDX"]:
                                key += f"/{data[0]}"
                                data = ",".join(data[1:])
                                await self._mqtt_client.publish(topic=f"{TOPIC}/ble/{key}", payload=data, qos=1, retain=False)
                            else:
                                for idx, value in enumerate(data):
                                    await self._mqtt_client.publish(topic=f"{TOPIC}/ble/{key}/{idx}", payload=value, qos=1, retain=False)

            finally:
                self._notification_buffer_rx = "";

    _notification_buffer_st = ""
    async def _notification_handler_st(self, sender, data):
        self._notification_buffer_st += data.decode()
        if "\n" in self._notification_buffer_st:
            log.info("_notification ST received: %s", self._notification_buffer_st)
            self._notification_buffer_st = "";


# Jun 12 20:05:53 ew2mqtt easywallbox-mqtt[98318]: 2025-06-12 20:05:53,411 MainThread      INFO     easywallbox    :172      _notification RX received: $BLE,LOGOUT$DATA,OK
async def main():
    mqtt_host = os.getenv('MQTT_HOST', '192.168.2.70')
    mqtt_port = os.getenv('MQTT_PORT', 1883)
    mqtt_username = os.getenv('MQTT_USERNAME', "")
    mqtt_password = os.getenv('MQTT_PASSWORD', "")

    queue = asyncio.Queue()

    # The callback for when a PUBLISH message is received from the server.
    async def on_message(client):
        await client.subscribe([
            # legacy
            (f"{TOPIC}/dpm",0), (f"{TOPIC}/charge",0), (f"{TOPIC}/limit",0), (f"{TOPIC}/read", 0)
            # settings
            ,(f"{TOPIC}/settings/+", 0)
            # evcc - status
            ,(EVCC_STATUS_SOURCE, 0)
            ,(EVCC_ENABLED_SOURCE, 0)
            # evcc - commands
            ,(EVCC_COMMAND_MAXCURRENT, 0), (EVCC_COMMAND_MAXCURRENTMILLIS, 0), (EVCC_COMMAND_ENABLE, 0)
            ])
        async for msg in client.messages:
            topic = str(msg.topic)
            message = msg.payload.decode()
            log.info(f"Message received [{topic}]: {message}")
            ble_command = None

            try:
                if topic == EVCC_STATUS_SOURCE:
                    status_code = int(message)
                    evcc_status = EVCC_STATUS_ERROR
                    if status_code == 3:
                        evcc_status =  EVCC_STATUS_AVAILABLE
                    elif status_code == 6 or status_code == 5:
                        evcc_status = EVCC_STATUS_VEHICLE_PRESENT
                    elif status_code == 8:
                        evcc_status = EVCC_STATUS_CHARGING
                    else:
                        log.warn(f"Unknown EVCC status: {status_code}")
                    await client.publish(f"{TOPIC}/evcc/status", evcc_status, qos=0, retain=False)
                    await client.publish(f"{TOPIC}/evcc/enabled", eb.is_evcc_enabled(), qos=0, retain=False)
                    break
                elif topic == EVCC_COMMAND_ENABLE:
                    await eb.evcc_enable(message == "true")
                    break
                elif topic == EVCC_COMMAND_MAXCURRENT:
                    max_current = int(message)
                    ble_command = commands.setUserLimit(max_current, millis=False)
                elif topic == EVCC_COMMAND_MAXCURRENTMILLIS:
                    # evcc will send a float number, multiply by 10 to get mA
                    max_current = int(round(float(message)*10))
                    ble_command = commands.setUserLimit(max_current, millis=True)

                elif topic.starts_with("settings/"):
                    opt = topic.split("/", maxsplit=1)[1]
                    log.info(f"Updating setting {opt}={message}")
                    try:
                        await settings.set(opt, int(message))
                    except ValueError as e:
                        log.error(f"Invalid settings value for {opt}: {e}")
                    finally:
                        break

                elif("/" in message):
                    msx = message.split("/") #limit/10
                    if msx[0] == "raw":
                        log.info(f"Raw command: {msx[1]}")
                        ble_command = msx[1]
                    else:
                        #TODO: Check if msx contains valid values
                        ble_command = mqttmap.MQTT2BLE[topic][msx[0]+"/"](msx[1])
                else:
                    ble_command = mqttmap.MQTT2BLE[topic][message]
            except Exception:
                log.error(f"Failure processing MQTT message.", exc_info=True)
                pass

            if(ble_command != None):
                await queue.put(ble_command)

    async with aiomqtt.Client(
        hostname=mqtt_host, port=mqtt_port,
        username=mqtt_username, password=mqtt_password,
        identifier="easywallbox-mqtt",
        keepalive=60,
        will=aiomqtt.Will(f"{TOPIC}/status/general", "offline"),
    ) as client:
        log.info("[MQTT] Connected.")

        eb = EasyWallbox(queue, client)

        await client.publish(f"{TOPIC}/status/general", "online", qos=1)
        await client.publish(f"{TOPIC}/evcc/enabled", "false", qos=1)

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT,
                            lambda: asyncio.create_task(on_exit(client, eb, queue)))

        # Avvia publisher e listener
        await asyncio.gather(
            on_message(client),
            queue_consumer(client, eb, queue),
        )

async def on_exit(client, eb, queue):
    global _cleanup_ran
    with _cleanup_lock:
        if _cleanup_ran:
            return

        _cleanup_ran = True
    log.info("Shutting down")
    await eb.stop()
    await client.publish(f"{TOPIC}/status/general", "offline", qos=1)
    await client.publish(f"{TOPIC}/evcc/enabled", "false", qos=1)

    sys.exit(0)

async def queue_consumer(client, eb, queue):
    try:
        while True:
            if not await eb.is_connected() and not await eb.is_connecting():
                log.info("Lost connection to BLE, reconnecting...")
                await eb.connect()
            elif queue.empty():
                pass
            else:
                item = await queue.get()
                log.info("Consuming ...")
                if item is None:
                    log.info("nothing to consume!")
                    break
                log.info(f"Consuming item {item}...")
                await eb.write(item)
            await asyncio.sleep(1)
    except Exception as e:
        log.error(f"Application error: {e}")
    finally:
        await on_exit(client, eb, queue)

def cli_main():
  try:
      asyncio.run(main())
  except asyncio.CancelledError:
      pass

if __name__ == "__main__":
    cli_main()
