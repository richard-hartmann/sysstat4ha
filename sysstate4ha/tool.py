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

log = logging.getLogger(__name__)


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

    def _generate_discovery_JSON(self):
        cmps = {}
        for i in range(1, self.ncpu + 1):
            cmps[f"cpu{i}"] = {
                "name": f"CPU{i}",
                "platform": "sensor",
                "state_topic": f"{self.mid}/cpu{i}",
                "unique_id": f"{self.mid}_cpu{i}",
                "unit_of_measurement": "%",
            }

        cmps[f"cpu_all"] = {
            "name": f"CPU all",
            "platform": "sensor",
            "state_topic": f"{self.mid}/cpu_all",
            "unique_id": f"cpu_all_{self.mid}",
            "unit_of_measurement": "%",
        }

        for dp in self.disk_parts:
            dev = dp.device.replace("/", "_")[1:]
            cmps[dev] = {
                "name": f"{dp.device} {dp.mountpoint} {dp.fstype}",
                "platform": "sensor",
                "state_topic": f"{self.mid}/{dev}",
                "unique_id": f"{self.mid}_{dev}",
                "unit_of_measurement": "%",
            }

        cmps["uptime"] = {
            "name": "uptime",
            "platform": "sensor",
            "state_topic": f"{self.mid}/uptime",
            "unique_id": f"{self.mid}_uptime",
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
                self._pub(psutil.cpu_percent(), f"{self.mid}/cpu_all")
                p = psutil.cpu_percent(percpu=True)
                for i in range(1, self.ncpu + 1):
                    self._pub(p[i - 1], f"{self.mid}/cpu{i}")
                for dp in self.disk_parts:
                    dev = dp.device.replace("/", "_")[1:]
                    self._pub(
                        psutil.disk_usage(dp.mountpoint).percent, f"{self.mid}/{dev}"
                    )
                self._pub(get_uptime(), f"{self.mid}/uptime")

                t1 = time.perf_counter()
                time.sleep(self.interval - (t1 - t0))
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


def cli():
    parser = argparse.ArgumentParser(
        prog="SysStat4HA",
        description="Expose system state, e.g., CPU load, uptime etc. to home assistant using mqtt (demonstrator project)",
    )

    parser.add_argument(
        "cmd",
        help="what to do",
        choices=["expose", "remove", "publish", "prepare_install"],
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
