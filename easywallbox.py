#!/usr/bin/env python3
import asyncio
import sys
from bleak import BleakClient
import paho.mqtt.client as mqtt
import os
import commands
import mqttmap
import time
import atexit
import signal
import logging
import settings
from repeating_timer import RepeatingTimer
from blemap import WALLBOX_EPROM as eeprom

FORMAT = ('%(asctime)-15s %(threadName)-15s '
          '%(levelname)-8s %(module)-15s:%(lineno)-8s %(message)s')
logging.basicConfig(format=FORMAT)
log = logging.getLogger()
log.setLevel(logging.INFO)


EVCC_STATUS_SOURCE = "easywallbox/ble/AD/4"
EVCC_ENABLED_SOURCE = "easywallbox/ble/IDX/158" # Wallbox DPM limit
EVCC_COMMAND_MAXCURRENT = "easywallbox/evcc/maxcurrent"
EVCC_COMMAND_MAXCURRENTMILLIS = "easywallbox/evcc/maxcurrentmillis"
EVCC_COMMAND_ENABLE = "easywallbox/evcc/enable"
EVCC_STATUS_AVAILABLE = "A"
EVCC_STATUS_VEHICLE_PRESENT = "B"
EVCC_STATUS_CHARGING = "C"
EVCC_STATUS_ERROR = "D"

mqttClient = None

settings = settings.Settings()

class EasyWallbox:
    WALLBOX_ADDRESS = os.getenv('WALLBOX_ADDRESS', '8C:F6:81:AD:B8:3E')
    WALLBOX_PIN = os.getenv('WALLBOX_PIN', '9844')

    WALLBOX_RX = "a9da6040-0823-4995-94ec-9ce41ca28833";
    WALLBOX_SERVICE = "331a36f5-2459-45ea-9d95-6142f0c4b307";
    WALLBOX_ST = "75A9F022-AF03-4E41-B4BC-9DE90A47D50B";
    WALLBOX_TX = "a73e9a10-628f-4494-a099-12efaf72258f";
    WALLBOX_UUID ="0A8C44F5-F80D-8141-6618-2564F1881650";

    def __init__(self, queue):
        self._client = BleakClient(self.WALLBOX_ADDRESS)
        self._queue = queue
        self._lock = asyncio.Lock()
        self.connecting = False
        self.state_timer = RepeatingTimer(self._update_ble_state)
        self._evcc_enabled = True

    def is_connected(self):
        return self._client.is_connected

    async def is_connecting(self):
      async with self._lock:
        return self.connecting

    async def write(self, data):
        if isinstance(data, str):
            data = bytearray(data, 'utf-8')
        await self._client.write_gatt_char(self.WALLBOX_RX, data)
        log.info("ble write: %s", data)

    async def connect(self):
        global client
        try:
            async with self._lock:
              self.connecting = True
              client.publish("easywallbox/status/ble", "connecting")
            log.info("Connecting BLE...")
            await self._client.connect()
            log.info(f"Connected on {self.WALLBOX_ADDRESS}: {self._client.is_connected}")

            log.info("Pairing BLE...")
            paired = sys.platform == "darwin" or await self._client.pair(protection_level=2)
            log.info(f"Paired: {paired}")

            await self._client.start_notify(self.WALLBOX_TX, self._notification_handler_rx) #TX NOTIFY
            log.info("TX NOTIFY STARTED")
            await self._client.start_notify(self.WALLBOX_ST, self._notification_handler_st) #ST NOTIFY (CANAL BUSMODE)
            log.info("ST NOTIFY STARTED")

            log.info("BLE AUTH START: %s", self.WALLBOX_PIN)
            await self.write(commands.authBle(self.WALLBOX_PIN))

            async with self._lock:
              self.connecting = False
              client.publish("easywallbox/status/ble", "connected", qos=1)

            refresh_timer = await settings.get(settings.KEY_BLE_REFRESH_INTERVAL)
            await asyncio.sleep(3)
            log.info("Starting ble state timer...")
            self.state_timer.start(refresh_timer)
        except Exception as e:
            log.error(f"Failed to connect or pair to device {self.WALLBOX_ADDRESS}: {e}")
            client.publish("easywallbox/status/ble", f"failed: {e}")
            retry_in_sec=await settings.get(settings.KEY_BLE_RETRY_INTERVAL)
            log.error("Connection attempts failed. Retrying in {} seconds.".format(retry_in_sec))
            await asyncio.sleep(retry_in_sec)
            async with self._lock:
              self.connecting = False

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

    def evcc_enable(self, enable=True):
        self._evcc_enabled = enable
        if enable:
            self._queue.put_nowait(commands.setDpmLimit(26*2))
        else:
            # this is very tricky - I could not find a way to disable/pause a charge process
            # so the only way is set the limit to 1A so that the DPM will not allow the charge to start
            self._queue.put_nowait(commands.setDpmLimit(1))

    def dpm_limit_changed(self, new_limit):
        self._evcc_enabled = new_limit != "10"

    def is_evcc_enabled(self):
        return self._evcc_enabled


    _notification_buffer_rx = ""
    def _notification_handler_rx(self, sender, data):
        global client
        self._notification_buffer_rx += data.decode()
        if "\n" in self._notification_buffer_rx:
            log.info("_notification RX received: %s", self._notification_buffer_rx)
            try:
                if (client):
                    client.publish(topic="easywallbox/message", payload=self._notification_buffer_rx, qos=1, retain=False)
                    chunks = self._notification_buffer_rx.split(",")
                    if len(chunks) > 3:
                        key = chunks[2]
                        data = chunks[3:]
                        if key in ["AL", "SL", "IDX"]:
                            key += f"/{data[0]}"
                            data = ",".join(data[1:])
                            client.publish(topic=f"easywallbox/ble/{key}", payload=data, qos=1, retain=True)
                        else:
                            for idx, value in enumerate(data):
                                client.publish(topic=f"easywallbox/ble/{key}/{idx}", payload=value, qos=1, retain=True)
            #self._queue.put_nowait(self._notification_buffer_rx)
            finally:
                self._notification_buffer_rx = "";

        #print(data.decode('utf-8'), end='', file=sys.stdout, flush=True)
        #self._queue.put_nowait(data.decode('utf-8'))

    _notification_buffer_st = ""
    def _notification_handler_st(self, sender, data):

        self._notification_buffer_st += data.decode()
        if "\n" in self._notification_buffer_st:
            log.info("_notification ST received: %s", self._notification_buffer_st)
            #self._queue.put_nowait(self._notification_buffer_st)
            self._notification_buffer_st = "";

        #print(data.decode('utf-8'), end='', file=sys.stdout, flush=True)
        #self._queue.put_nowait(data.decode('utf-8'))

async def main():
    global client
    mqtt_host = os.getenv('MQTT_HOST', '192.168.2.70')
    mqtt_port = os.getenv('MQTT_PORT', 1883)
    mqtt_username = os.getenv('MQTT_USERNAME', "")
    mqtt_password = os.getenv('MQTT_PASSWORD', "")

    queue = asyncio.Queue()

    eb = EasyWallbox(queue)

    # The callback for when the client receives a CONNACK response from the server.
    def on_connect(client, userdata, flags, rc):
        print("Connected to MQTT Broker with result code "+str(rc))
        client.connected_flag=True
        log.info("Connected to MQTT Broker!")
        # Subscribing in on_connect() means that if we lose the connection and
        # reconnect then subscriptions will be renewed.
        client.subscribe([
            # legacy
            ("easywallbox/dpm",0), ("easywallbox/charge",0), ("easywallbox/limit",0), ("easywallbox/read", 0)
            # settings
            ,("easywallbox/settings/+", 0)
            # evcc - status
            ,(EVCC_STATUS_SOURCE, 0)
            ,(EVCC_ENABLED_SOURCE, 0)
            # evcc - commands
            ,(EVCC_COMMAND_MAXCURRENT, 0), (EVCC_COMMAND_MAXCURRENTMILLIS, 0), (EVCC_COMMAND_ENABLE, 0)
            ])


    # The callback for when a PUBLISH message is received from the server.
    def on_message(client, userdata, msg):
        #print(msg.topic+" "+str(msg.payload))
        #queue.put_nowait(msg.payload)

        topic = msg.topic
        message = msg.payload.decode()
        log.info(f"Message received [{topic}]: {message}")
        ble_command = None

        try:
            if topic == EVCC_STATUS_SOURCE:
                status_code = int(message)
                evcc_status = EVCC_STATUS_ERROR
                if status_code == 3:
                    evcc_status =  EVCC_STATUS_AVAILABLE
                elif status_code == 6:
                    evcc_status = EVCC_STATUS_VEHICLE_PRESENT
                elif status_code == 8:
                    evcc_status = EVCC_STATUS_CHARGING
                else:
                    log.warning(f"Unknown EVCC status: {status_code}")
                client.publish("easywallbox/evcc/status", evcc_status, qos=0, retain=False)
                client.publish("easywallbox/evcc/enabled", eb.is_evcc_enabled(), qos=0, retain=False)
                return
            elif topic == EVCC_COMMAND_ENABLE:
                eb.evcc_enable(message == "true")
                return
            elif topic == EVCC_COMMAND_MAXCURRENT:
                max_current = int(message)
                ble_command = commands.setUserLimit(max_current, millis=False)
            elif topic == EVCC_COMMAND_MAXCURRENTMILLIS:
                max_current = int(message)
                ble_command = commands.setUserLimit(max_current, millis=True)

            elif "settings/" in topic:
                opt = topic.split("/", maxsplit=1)[1]
                log.info(f"Updating setting {opt}={message}")
                try:
                    settings.set_nowait(opt, int(message))
                except ValueError as e:
                    log.error(f"Invalid settings value for {opt}: {e}")
                finally:
                    return

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
            pass

        print(ble_command)

        if(ble_command != None):
            queue.put_nowait(ble_command)

    async def on_exit():
        log.info("Shutting down")
        eb.state_timer.stop()
        client.publish("easywallbox/status/general", "offline", qos=1)
        client.publish("easywallbox/evcc/enabled", "false", qos=1)
        # restore limits
        queue.put_nowait(commands.setDpmLimit(27))
        queue.put_nowait(commands.setUserLimit(26))

        await asyncio.sleep(5)
        await eb._client.disconnect()
        sys.exit(0)

    mqtt.Client.connected_flag=False

    client = mqtt.Client("mqtt-easywallbox")
    client.on_connect = on_connect
    client.on_message = on_message
    client.loop_start()
    client.username_pw_set(username=mqtt_username,password=mqtt_password)
    client.connect(mqtt_host, mqtt_port, 60)

    while not client.connected_flag: #wait in loop
        log.info("...")
        time.sleep(1)

    client.publish("easywallbox/status/general", "online", qos=1)
    client.publish("easywallbox/evcc/enabled", "false", qos=1)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGINT,
                            lambda: asyncio.create_task(on_exit()))
    atexit.register(on_exit)


    #def read_line():
    #    line = sys.stdin.readline()
    #    if line:
    #        queue.put_nowait(line)

    #task = loop.add_reader(sys.stdin.fileno(), read_line)



    try:
        while True:
            if not eb.is_connected() and not await eb.is_connecting():
                log.info("Lost connection to BLE, reconnecting...")
                await eb.connect()
            elif queue.empty():
                pass
            else:
                #item = await queue.get()
                item = queue.get_nowait()
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
        await on_exit()

def cli_main():
  try:
      asyncio.run(main())
  except asyncio.CancelledError:
      pass

if __name__ == "__main__":
    cli_main()
