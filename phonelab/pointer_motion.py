"""Finite pointer approach using observed, device-specific signed pixel gains."""
import math


def _pair(value, *, gains=False):
    if (not isinstance(value,(tuple,list)) or len(value)!=2
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in value)
            or (gains and any(not .25<=abs(v)<=4 for v in value))):
        raise ValueError('valid observed pointer calibration required')
    return value


def plan_pointer_step(position,goal,pixels_per_unit):
    position=_pair(position);goal=_pair(goal);gains=_pair(pixels_per_unit,gains=True)
    # Simultaneous unheld axes shorten the approach without increasing either
    # axis limit. The caller must still independently guard every input write.
    return {key:(max(-16,min(16,round((goal[i]-position[i])/gains[i])))
                 if abs(goal[i]-position[i])>6 else 0)
            for i,key in enumerate(('dx','dy'))}


def verify_pointer_step(before,after,command,pixels_per_unit):
    before=_pair(before);after=_pair(after);gains=_pair(pixels_per_unit,gains=True)
    if (not isinstance(command,dict) or set(command)!={'dx','dy'}
            or any(type(v) is not int or abs(v)>16 for v in command.values())):
        raise ValueError('bounded pointer movement required')
    for i,key in enumerate(('dx','dy')):
        expected=command[key]*gains[i];actual=after[i]-before[i]
        if expected and actual*(1 if expected>0 else -1)<-3:
            raise ValueError('pointer direction contradicts observed calibration')
    return True
