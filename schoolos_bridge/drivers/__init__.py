from .base import DeviceDriver, DeviceProbe, DriverError, FaceMatch, Person, RawEvent, TerminalPerson


def build_driver(cfg) -> DeviceDriver:
    if cfg.vendor == "hikvision":
        from .hikvision import HikvisionDriver

        return HikvisionDriver(cfg)
    raise ValueError(f"unknown vendor {cfg.vendor}")


__all__ = ["DeviceDriver", "DeviceProbe", "DriverError", "FaceMatch", "Person", "RawEvent", "TerminalPerson", "build_driver"]
