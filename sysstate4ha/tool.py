import psutil
import json
from pathlib import Path
import logging
import subprocess
import time
import argparse
import socket
import datetime as dt
import tomllib
import os
from dataclasses import dataclass
from typing import Callable
from functools import partial
import yaml
import re

log = logging.getLogger(__name__)


class CPUUsage:
    """use psutil to get CPU usage

    key feature: getter returns a function with no arguments that returns the cpu usage for
    a single core (what is an integer) or the average (what is 'all'). Since psutil's
    cpu_percent yields average values with respect to the last call, values are cached until
    an already fetched quantity is fetched again.
    """

    def __init__(self):
        self.update()

    def get(self, what):
        if what in self.fetched:
            self.update()
        self.fetched.add(what)
        if what == "all":
            return self.cpu_usage_avrg
        else:
            return self.cpu_usage[what]

    def getter(self, what):
        return partial(self.get, what=what)

    def update(self):
        self.fetched = set()
        self.cpu_usage = psutil.cpu_percent(percpu=True)
        self.cpu_usage_avrg = int(10*sum(self.cpu_usage) / len(self.cpu_usage)) / 10
        log.debug("new cpu_percent data cached")


def get_machiene_id(len_id=6):
    if (p := Path("/etc/machine-id")).exists():
        with open(p, "r") as f:
            mid = f.readline()[:len_id]
            log.debug(f"machine id: {mid} ({p})")
            return mid
    # implement alternative ID retrievals
    ...

    raise RuntimeError("Failed to get machine ID")


def get_machine_product_name():
    # product_name seems available on many unix systems
    if (p := Path("/sys/devices/virtual/dmi/id/product_name")).exists():
        with open(p, "r") as f:
            pn = f.readline()
            log.info(f"machine product name: {pn} ({p})")
            return pn
    # fall back for Raspberry Pi
    with open("/proc/cpuinfo", "r") as f:
        for l in f:
            pass
        if l.startswith("Model"):
            pn = l.split(":")[1].strip()
            log.info(f"machine product name: {pn} (last line of /proc/cpuinfo)")
            return pn

    raise RuntimeError("Failed to get machine product name")


def get_uptime():
    if (p := Path("/proc/uptime")).exists():
        with open(p, "r") as f:
            ut_sec = int(float(f.readline().split()[0]))
            return str(dt.timedelta(seconds=ut_sec))

    RuntimeError("Failed to get uptime")


class Entity:
    """abstraction layer for entities to generalize"""

    def __init__(
        self,
        name: str,
        mid: str,
        unit_of_measurement: str,
        get: Callable,
        yaml_keys: dict = {},
        full_name: str | None = None,
        card_name: str | None = None,
    ):
        self.name = name
        self.mid = mid
        self.unit_of_measurement = unit_of_measurement
        self.get = get
        self.yaml_keys = yaml_keys
        self.full_name = full_name
        self.card_name = name if card_name is None else card_name

        self.qual_name = self.name.replace(" ", "_").replace("/", "_").lower()
        self.state_topic = f"{self.mid}/{self.qual_name}"
        self.unique_id = f"{self.mid}_{self.qual_name}"
        


class SysState4HA:
    def __init__(
        self,
        ha_host,
        mqtt_user,
        mqtt_password,
        update_interval=2,
        len_id=6,
        host_alias="",
        origin_name="linux mosquitto",
    ):
        self.mid = get_machiene_id(len_id)
        self.prod_name = get_machine_product_name()
        self.host_alias = socket.gethostname() if host_alias == "" else host_alias
        self.origin_name = origin_name
        self.host = ha_host
        self.user = mqtt_user
        self.passowrd = mqtt_password
        self.interval = update_interval

        self.ncpu = psutil.cpu_count()
        self.disk_parts = psutil.disk_partitions()

        self.base_cmd = (
            f"mosquitto_pub -h {self.host} -r -u {self.user} -P {self.passowrd}"
        )

        self.cpu_usage = CPUUsage()

        self.entities: list[Entity] = []
        # overall CPU usage
        self.entities.append(
            Entity(
                name="CPU all",
                mid=self.mid,
                unit_of_measurement="%",
                get=self.cpu_usage.getter("all"),
                yaml_keys={
                    "type": "custom:bar-card",
                    "entity_row": "true",
                    "icon": "mdi:chip",
                },
            )
        )
        # individual cores
        for i in range(1, psutil.cpu_count() + 1):
            self.entities.append(
                Entity(
                    name=f"CPU {i}",
                    mid=self.mid,
                    unit_of_measurement="%",
                    get=self.cpu_usage.getter(i - 1),
                    yaml_keys={
                        "type": "custom:bar-card",
                        "entity_row": "true",
                        "icon": "mdi:chip",
                    },
                )
            )
        # disk usage
        for dp in self.disk_parts:
            self.entities.append(
                Entity(
                    # used to construct the state_topic and sensor id
                    name=dp.device,
                    # this will be shown in the generated card YAML
                    card_name = dp.mountpoint,
                    # extra info, not used yet
                    full_name=f"{dp.device}: {dp.mountpoint} ({dp.fstype})",
                    mid=self.mid,
                    unit_of_measurement="%",
                    get=partial(
                        lambda x: psutil.disk_usage(x).percent, x=dp.mountpoint
                    ),
                    yaml_keys={
                        "type": "custom:bar-card",
                        "entity_row": "true",
                        "icon": "mdi:harddisk",
                    },
                )
            )
        # uptime
        self.entities.append(
            Entity(
                name="uptime", mid=self.mid, unit_of_measurement=None, get=get_uptime,
                yaml_keys={'icon': 'mdi:timelapse'}
            )
        )

    def _generate_discovery_JSON(self):
        cmps = {}
        for e in self.entities:
            cmps[e.name] = {
                "name": e.name,
                "platform": "sensor",
                "state_topic": e.state_topic,
                "unique_id": e.unique_id,
                "unit_of_measurement": e.unit_of_measurement,
            }

        data = {
            "origin": {"name": self.origin_name},
            "components": {**cmps},
            "device": {
                "name": self.host_alias,
                "identifiers": [self.mid],
                "model": self.prod_name,
            },
        }

        return json.dumps(data, sort_keys=True, indent=4)

    #####################################
    ## commands
    #####################################

    def expose(self, **kwargs):
        js = self._generate_discovery_JSON().replace('"', r"\"")
        cmd = f'{self.base_cmd} -t homeassistant/device/{self.mid}/config -m "{js}"'
        log.debug(f"run cmd: {cmd}")
        subprocess.run(cmd, shell=True, check=True, capture_output=False)
        log.info(f"expose device/{self.mid} successful")

    def remove(self, **kwargs):
        cmd = f'{self.base_cmd} -t homeassistant/device/{self.mid}/config -m ""'
        log.debug(f"run cmd: {cmd}")
        subprocess.run(cmd, shell=True, check=True, capture_output=False)
        log.info(f"remove device/{self.mid} successful")

    def _pub(self, value, topic):
        cmd = f"{self.base_cmd} -t {topic} -m {value}"
        log.debug(f"run cmd: {cmd}")
        r = subprocess.run(cmd, shell=True, capture_output=False)
        if r.returncode == 0:
            log.debug(f"publish {value} to {topic} sucessfull")
        else:
            log.warning(f"failed to publish {value} to {topic} ({r.stderr})")

    def publish(self, **kwargs):
        try:
            while True:
                t0 = time.perf_counter()
                for e in self.entities:
                    self._pub(e.get(), e.state_topic)

                t1 = time.perf_counter()
                time.sleep(s if (s := self.interval - (t1 - t0)) > 0 else 0)
        except KeyboardInterrupt:
            pass

    def prepare_install(self, conf, **kwargs):
        conf = Path(conf).absolute()
        r = subprocess.run(
            "ps --no-headers -o comm 1",
            shell=True,
            check=True,
            capture_output=True,
            text=True,
        )
        if r.stdout.strip() != "systemd":
            log.error(f"'ps --no-headers -o comm 1 yields' {r.stdout}")
            raise RuntimeError("expect linux system with systemd")

        package_root = Path(__file__).parent.parent.absolute()

        r = subprocess.run(
            "whereis poetry", shell=True, check=True, capture_output=True, text=True
        )
        poetry_path = r.stdout.split(":")[1].strip()

        user = os.getlogin()

        service_file = f"""[Unit]
Description=Publish System State to Home Assistant using MQTT.

[Service]
ExecStart={poetry_path} -C {package_root} run s4h publish -c {conf}
Restart=no
User={user}

[Install]
WantedBy=multi-user.target
"""
        p = package_root / "installer"
        p.mkdir(exist_ok=True)
        with open(p / "s4h.service", "w") as f:
            f.write(service_file)

        install_script = f"""#!/bin/bash
cp {p / "s4h.service"} /etc/systemd/system/
systemctl daemon-reload
systemctl enable s4h.service
systemctl start s4h.service

"""
        p_install = p / "install.sh"
        with open(p_install, "w") as f:
            f.write(install_script)

        p_install.chmod(0o755)
        print(f"call 'sudo {p_install}' to install publishing as systemd service")

        entities_list = []
        for e in self.entities:
            entity_qual_name = f"sensor.{self.host_alias}_{e.qual_name}"
            # reduce multiple '_' to only one
            entity_qual_name = re.sub(r'_+', '_', entity_qual_name)
            # remove trailing '_'
            entity_qual_name = re.sub(r'_$', '', entity_qual_name)

            entities_list.append(
                {
                    "entity": entity_qual_name,
                    "name": e.card_name,
                    **e.yaml_keys,
                }
            )

        card_dict = {"type": "entities", "entities": entities_list}
        p_card_yaml = p / "card.yaml"
        with open(p_card_yaml, 'w') as f:
            yaml.dump(card_dict, f)

        print(f"check out {p_card_yaml} for a card proposal (needs 'custom:bar-card' from HACS)")


def cli():
    parser = argparse.ArgumentParser(
        prog="SysStat4HA",
        description="Expose system state, e.g., CPU load, uptime etc. to home assistant using mqtt (demonstrator project)",
    )

    parser.add_argument(
        "cmd",
        help="what to do",
        choices=["expose", "remove", "publish", "prepare_install", "test"],
    )
    parser.add_argument("-c", "--conf", help="path to config toml", required=True)
    parser.add_argument(
        "-l",
        "--loglevel",
        help="see python's logging module for details about the loglevel",
        choices=["notset", "debug", "info", "warning", "error", "critical"],
        default="error",
    )

    args = parser.parse_args()

    logging.basicConfig()
    log.setLevel(args.loglevel.upper())

    with open(args.conf, "rb") as f:
        conf = tomllib.load(f)

    h = SysState4HA(**conf["SysState4HA"])
    getattr(h, args.cmd)(**vars(args))


if __name__ == "__main__":
    cli()
