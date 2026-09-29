from __future__ import annotations
from dataclasses import dataclass
import math

@dataclass(frozen=True)
class VoltVarPoint:
    voltage_pu: float
    kvar_fraction: float

@dataclass(frozen=True)
class InverterCapability:
    kva: float
    kw_available: float
    kvar_limit: float
    grid_forming_capable: bool = False


def interpolate_curve(points:list[VoltVarPoint],voltage_pu:float):
    if not points: return 0.0
    pts=sorted(points,key=lambda p:p.voltage_pu)
    if voltage_pu<=pts[0].voltage_pu: return pts[0].kvar_fraction
    if voltage_pu>=pts[-1].voltage_pu: return pts[-1].kvar_fraction
    for a,b in zip(pts,pts[1:]):
        if a.voltage_pu<=voltage_pu<=b.voltage_pu:
            f=(voltage_pu-a.voltage_pu)/(b.voltage_pu-a.voltage_pu)
            return a.kvar_fraction+f*(b.kvar_fraction-a.kvar_fraction)
    return 0.0


def capability_kvar(cap:InverterCapability,kw:float):
    kw=min(abs(kw),cap.kva)
    circle=max(0.0,cap.kva*cap.kva-kw*kw)**0.5
    return min(cap.kvar_limit,circle)


def volt_var_setpoint(cap:InverterCapability,kw:float,voltage_pu:float,curve:list[VoltVarPoint]):
    fraction=interpolate_curve(curve,voltage_pu)
    limit=capability_kvar(cap,kw)
    return max(-limit,min(limit,fraction*cap.kva))


def watt_limit_from_voltage(available_kw:float,voltage_pu:float,start:float=1.06,stop:float=1.10):
    if voltage_pu<=start: return available_kw
    if voltage_pu>=stop: return 0.0
    return available_kw*(stop-voltage_pu)/(stop-start)


def apparent_power(kw:float,kvar:float): return math.hypot(kw,kvar)
