"""Minimal Redfish client for server BMCs such as iBMC, iDRAC, iLO and XClarity (power readings and fencing)."""
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class Redfish:
    def __init__(self, bmc):
        self.base = f"https://{bmc['address']}"
        self.s = requests.Session()
        self.s.auth = (bmc["user"], bmc["password"])
        self.s.verify = bmc.get("verify_tls", False)
        self.timeout = bmc.get("timeout", 10)
        self._system = None

    def _get(self, path):
        r = self.s.get(self.base + path, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def system(self):
        if not self._system:
            self._system = self._get("/redfish/v1/Systems")["Members"][0]["@odata.id"]
        return self._system

    def power_state(self):
        return self._get(self.system()).get("PowerState")

    def reset(self, kind):
        """kind: ForceOff | On | ForceRestart | GracefulShutdown"""
        r = self.s.post(f"{self.base}{self.system()}/Actions/ComputerSystem.Reset",
                        json={"ResetType": kind}, timeout=self.timeout)
        r.raise_for_status()

    def watts(self):
        chassis = self._get("/redfish/v1/Chassis")["Members"][0]["@odata.id"]
        power = self._get(chassis + "/Power")
        ctl = power.get("PowerControl") or [{}]
        return ctl[0].get("PowerConsumedWatts")
